import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from competitor_tracker import cli
from competitor_tracker.analyzer import AnalysisResult, CompetitorAlertAnalyzer
from competitor_tracker.config import TrackerConfig, TrackerRuntimeConfig
from competitor_tracker.formatter import format_daily_digest
from competitor_tracker.models import ArticleContext, CandidateArticle, RawArticle
from competitor_tracker.providers import ProviderError
from competitor_tracker.storage import SQLiteTrackerStorage


@pytest.fixture(autouse=True)
def freeze_integration_today(monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: date.fromisoformat("2026-05-26"))


def freeze_cli_today(monkeypatch, *, iso_date: str = "2026-05-21") -> None:
    monkeypatch.setattr(cli, "_today", lambda: date.fromisoformat(iso_date))


def build_config(
    *,
    daily_digest_limit: int = 10,
    extra_topics: dict | None = None,
    competitors_by_region: dict | None = None,
    ignored_geo_terms: list[str] | None = None,
) -> TrackerConfig:
    topic_groups = {
        "market_entry": ["launch", "new city", "entering market"],
        "campaign_launches": ["campaign", "partnership", "brand ambassador", "new feature"],
        "pricing_promo": ["discount", "promo code", "price cut", "subscription"],
    }
    topic_groups.update(extra_topics or {})
    return TrackerConfig.from_dict(
        {
            "regions": {
                "sea": {
                    "label": "Southeast Asia",
                    "geo_terms": ["Philippines", "Indonesia", "Thailand"],
                    "country_validation_terms": [
                        "Philippines",
                        "PH",
                        "Indonesia",
                        "ID",
                        "Thailand",
                        "TH",
                        "Malaysia",
                        "MY",
                        "Singapore",
                        "SG",
                        "Vietnam",
                        "VN",
                    ],
                    "language_hints": ["en"],
                }
            },
            "competitors_by_region": competitors_by_region or {"sea": ["Grab", "Gojek", "Bolt"]},
            "topic_groups": topic_groups,
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "ignored_geo_terms": ignored_geo_terms or [
                "USA",
                "United States",
                "North America",
                "Europe",
                "UK",
            ],
            "daily_digest_limit": daily_digest_limit,
            "enabled_providers": ["google_news_rss", "gdelt"],
        }
    )


def build_runtime(tmp_path: Path) -> TrackerRuntimeConfig:
    return TrackerRuntimeConfig(
        output_dir=tmp_path / "output",
        database_path=tmp_path / "output" / "tracker.db",
        lookback_days=7,
        min_score=5,
        config_path=tmp_path / "config.json",
        use_llm_alerts=False,
        llm_top_n=15,
        telegram_top_n=15,
        article_context_max_chars=8000,
    )


def article(
    *,
    title: str,
    url: str,
    competitor: str,
    query: str = '"Grab" market entry Southeast Asia',
    published_at: str = "2026-05-19T09:00:00Z",
    snippet: str = "",
    source: str = "Example News",
    region: str = "sea",
    metadata: dict | None = None,
) -> RawArticle:
    return RawArticle(
        title=title,
        url=url,
        provider="mock_provider",
        source=source,
        published_at=published_at,
        snippet=snippet or title,
        query=query.replace("Grab", competitor),
        region=region,
        language="en",
        competitor_hints=(competitor,),
        metadata=metadata or {},
    )


def low_signal_article(
    *,
    title: str,
    url: str,
    competitor: str,
    published_at: str,
    snippet: str = "",
) -> RawArticle:
    return RawArticle(
        title=title,
        url=url,
        provider="mock_provider",
        source="Example News",
        published_at=published_at,
        snippet=snippet or title,
        query=f'"{competitor}" campaign',
        region=None,
        language=None,
        competitor_hints=(),
    )


class StaticProvider:
    def __init__(self, name: str, articles: list[RawArticle]) -> None:
        self.name = name
        self._articles = list(articles)

    def fetch(self, request):
        return list(self._articles)


class FailingProvider:
    def __init__(self, name: str = "failing_provider") -> None:
        self.name = name

    def fetch(self, request):
        raise ProviderError("temporary upstream failure")


def build_capped_articles(count: int) -> list[RawArticle]:
    competitors = [f"Comp{index}" for index in range(count)]
    descriptors = [
        ("launch bamboo ferry permits", "Manila"),
        ("launch skyline shuttle vouchers", "Jakarta"),
        ("launch harbor scooter program", "Bangkok"),
        ("launch desert airport relay", "Cebu"),
        ("launch coral commuter pass", "Davao"),
        ("launch volcano campus rides", "Surabaya"),
        ("launch river night transfer", "Pattaya"),
        ("launch lantern business lanes", "Chiang Mai"),
        ("launch island women driver push", "Iloilo"),
        ("launch stadium family mobility", "Bandung"),
        ("launch electric tuk route", "Phuket"),
        ("launch bazaar school pickup", "Yogyakarta"),
        ("launch mountain tourist shuttle", "Baguio"),
        ("launch port cargo driver bonus", "Makassar"),
        ("launch university safety convoy", "Semarang"),
        ("launch temple hotel transfer", "Solo"),
    ]
    articles = []
    for index, competitor in enumerate(competitors):
        descriptor, city = descriptors[index]
        title = f"{competitor} {descriptor} in {city}"
        articles.append(
            article(
                competitor=competitor,
                title=title,
                url=f"https://example.com/{competitor.lower()}-market-entry-{index}",
                query=f'"{competitor}" market entry Southeast Asia',
                snippet=title,
            )
        )
    return articles


def patch_runtime(
    monkeypatch,
    tmp_path,
    config: TrackerConfig,
    *,
    use_llm_alerts: bool | None = None,
    llm_top_n: int | None = None,
    telegram_top_n: int | None = None,
    article_context_max_chars: int | None = None,
):
    runtime = build_runtime(tmp_path)
    if use_llm_alerts is not None:
        runtime = TrackerRuntimeConfig(
            output_dir=runtime.output_dir,
            database_path=runtime.database_path,
            lookback_days=runtime.lookback_days,
            min_score=runtime.min_score,
            config_path=runtime.config_path,
            use_llm_alerts=use_llm_alerts,
            llm_top_n=runtime.llm_top_n if llm_top_n is None else llm_top_n,
            telegram_top_n=(
                runtime.telegram_top_n if telegram_top_n is None else telegram_top_n
            ),
            article_context_max_chars=(
                runtime.article_context_max_chars
                if article_context_max_chars is None
                else article_context_max_chars
            ),
        )
    elif (
        llm_top_n is not None
        or telegram_top_n is not None
        or article_context_max_chars is not None
    ):
        runtime = TrackerRuntimeConfig(
            output_dir=runtime.output_dir,
            database_path=runtime.database_path,
            lookback_days=runtime.lookback_days,
            min_score=runtime.min_score,
            config_path=runtime.config_path,
            use_llm_alerts=runtime.use_llm_alerts,
            llm_top_n=runtime.llm_top_n if llm_top_n is None else llm_top_n,
            telegram_top_n=(
                runtime.telegram_top_n if telegram_top_n is None else telegram_top_n
            ),
            article_context_max_chars=(
                runtime.article_context_max_chars
                if article_context_max_chars is None
                else article_context_max_chars
            ),
        )
    monkeypatch.setattr(cli.TrackerRuntimeConfig, "from_env", staticmethod(lambda: runtime))
    monkeypatch.setattr(cli.TrackerConfig, "load", staticmethod(lambda path: config))
    return runtime


def test_full_dry_run_with_mocked_providers_writes_artifacts(tmp_path, monkeypatch):
    config = build_config()
    runtime = patch_runtime(monkeypatch, tmp_path, config)
    providers = [
        StaticProvider(
            "mock_news",
            [
                article(
                    competitor="Grab",
                    title="Grab launches new city campaign in Manila",
                    url="https://example.com/grab-manila-launch",
                    snippet="Grab launches a new city campaign in Manila with driver messaging.",
                )
            ],
        )
    ]
    monkeypatch.setattr(cli, "build_providers", lambda names: providers)

    sender_calls = {}
    notion_calls = {}

    class FakeTelegramSender:
        def __init__(self, storage, dry_run):
            sender_calls["dry_run"] = dry_run

        def send_daily_digest(self, alert_schemas, alerts, source_urls, generated_at):
            sender_calls["alerts"] = len(alerts)
            return {"ok": True, "dry_run": True}

    class FakeNotionSync:
        def sync_alerts(self, items, dry_run=False):
            notion_calls["dry_run"] = dry_run
            notion_calls["items"] = len(items)
            return {"created": 0, "updated": 0, "skipped": 0, "would_create": len(items), "would_update": 0}

    monkeypatch.setattr(cli, "TelegramSender", FakeTelegramSender)
    monkeypatch.setattr(cli, "CompetitorNotionMirrorSync", FakeNotionSync)

    result = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="dry",
        notion_mode="dry",
        export_csv=True,
    )

    assert len(result["digest"].alerts) == 1
    assert sender_calls == {"dry_run": True, "alerts": 1}
    assert notion_calls == {"dry_run": True, "items": 1}
    assert result["candidates_path"].exists()
    assert result["digest_path"].exists()
    assert result["preview_path"].exists()
    assert result["candidates_csv_path"].exists()
    assert result["summary_path"].exists()
    assert runtime.database_path.exists()

    preview_text = result["preview_path"].read_text(encoding="utf-8")
    assert "Ежедневный превью-дайджест competitor tracker" in preview_text
    assert "### Что произошло" in preview_text

    with sqlite3.connect(runtime.database_path) as connection:
        row = connection.execute(
            """
            SELECT
                started_at,
                finished_at,
                raw_articles_fetched,
                raw_articles_deduplicated,
                articles_filtered_out,
                alerts_sent,
                status
            FROM runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert row is not None
    assert row[0]
    assert row[1]
    assert row[2] == 1
    assert row[3] == 1
    assert row[4] == 0
    assert row[5] == 0
    assert row[6] == "success"


def test_run_summary_uses_consistent_utc_timestamps_in_json_and_sqlite(
    tmp_path, monkeypatch
):
    config = build_config()
    runtime = patch_runtime(monkeypatch, tmp_path, config)
    providers = [
        StaticProvider(
            "mock_news",
            [
                article(
                    competitor="Grab",
                    title="Grab launches Manila campaign",
                    url="https://example.com/grab-manila-timestamps",
                    snippet="Grab launches a new city campaign in Manila.",
                )
            ],
        )
    ]
    monkeypatch.setattr(cli, "build_providers", lambda names: providers)

    timestamps = iter(
        [
            "2026-05-26T01:00:00+00:00",
            "2026-05-26T01:02:00+00:00",
            "2026-05-26T01:05:30+00:00",
        ]
    )
    monkeypatch.setattr(cli, "_utc_now_iso", lambda: next(timestamps))

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    summary_payload = json.loads(result["summary_path"].read_text(encoding="utf-8"))
    assert summary_payload["started_at"] == "2026-05-26T01:00:00+00:00"
    assert summary_payload["finished_at"] == "2026-05-26T01:05:30+00:00"

    with sqlite3.connect(runtime.database_path) as connection:
        row = connection.execute(
            "SELECT started_at, finished_at FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert row == (
        "2026-05-26T01:00:00+00:00",
        "2026-05-26T01:05:30+00:00",
    )

    started_at = datetime.fromisoformat(summary_payload["started_at"])
    finished_at = datetime.fromisoformat(summary_payload["finished_at"])
    assert started_at.tzinfo is not None
    assert finished_at.tzinfo is not None
    assert started_at.utcoffset() == timedelta(0)
    assert finished_at.utcoffset() == timedelta(0)
    assert finished_at > started_at


def test_duplicate_suppression_across_runs_does_not_use_non_delivered_history(
    tmp_path, monkeypatch
):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)
    shared_articles = [
        article(
            competitor="Grab",
            title="Grab launches airport partnership in Manila",
            url="https://example.com/grab-airport-manila",
        )
    ]
    monkeypatch.setattr(cli, "build_providers", lambda names: [StaticProvider("mock_news", shared_articles)])

    first = cli.run_pipeline(days=7, min_score=5, regions=["sea"])
    second = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert len(first["digest"].alerts) == 1
    assert len(second["digest"].alerts) == 1


def test_run_pipeline_persists_rss_feed_metrics_for_later_qa(tmp_path, monkeypatch):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "mea": {
                    "label": "Middle East",
                    "geo_terms": ["Qatar"],
                    "country_validation_terms": ["Qatar", "QA", "UAE", "AE"],
                    "language_hints": ["en"],
                }
            },
            "competitors_by_region": {"mea": ["Uber"]},
            "topic_groups": {"pricing_promo": ["discount"]},
            "topic_priority_groups": ["pricing_promo"],
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "daily_digest_limit": 10,
            "enabled_providers": ["regional_rss"],
        }
    )
    runtime = patch_runtime(monkeypatch, tmp_path, config)

    class RegionalFeedProvider:
        name = "regional_rss"

        def fetch_with_diagnostics(self, request):
            article_item = RawArticle(
                title="Uber launches rider discount partnership in Doha",
                url="https://example.com/uber-doha-discount",
                provider="regional_rss",
                source="Doha News",
                published_at="2026-05-19T09:00:00Z",
                snippet="Uber launches a new rider promotion in Doha.",
                query="regional_rss::mea::Uber",
                region="mea",
                language="en",
                competitor_hints=("Uber",),
                metadata={
                    "query_owner_competitor": "Uber",
                    "query_owner_region": "mea",
                    "source_tier": "tier2_direct",
                    "direct_feed_name": "Doha News",
                    "direct_feed_url": "https://dohanews.co/feed/",
                },
            )
            return [article_item], {
                "provider": self.name,
                "status": "ok",
                "queries": [
                    {
                        "provider": self.name,
                        "query": "mea:Doha News",
                        "request_url": "https://dohanews.co/feed/",
                        "http_status": 200,
                        "exception": "",
                        "items_found": 9,
                        "items_after_filter": 1,
                        "status": "ok",
                        "feed_name": "Doha News",
                        "feed_url": "https://dohanews.co/feed/",
                        "feed_region": "mea",
                    }
                ],
                "items_found": 9,
                "items_after_filter": 1,
                "items_after_global_dedup": 0,
                "feeds_skipped": 0,
            }

    monkeypatch.setattr(cli, "build_providers", lambda names: [RegionalFeedProvider()])

    result = cli.run_pipeline(days=7, min_score=5, regions=["mea"])

    assert result["feed_metrics"][0]["feed_name"] == "Doha News"
    assert result["feed_metrics"][0]["alerts_created"] == 1

    storage = SQLiteTrackerStorage(runtime.database_path)
    report = storage.get_feed_health_report(days=365, min_items_found=1, limit=10)

    assert report["feed_count"] == 1
    assert report["feeds"][0]["feed_name"] == "Doha News"
    assert report["feeds"][0]["alerts_created"] == 1


def test_pipeline_filters_out_grab_article_from_usa_even_when_found_by_sea_query(tmp_path, monkeypatch):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)
    providers = [
        StaticProvider(
            "mock_news",
            [
                article(
                    competitor="Grab",
                    title="Grab launches new airport pricing program in the United States",
                    url="https://example.com/grab-united-states-airport-pricing",
                    query='"Grab" pricing Southeast Asia',
                    snippet="Grab is piloting discount airport rides across the USA market.",
                    region="sea",
                )
            ],
        )
    ]
    monkeypatch.setattr(cli, "build_providers", lambda names: providers)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert result["digest"].alerts == ()
    assert result["analysis"].candidates == []
    assert result["analysis"].dropped_count == 1
    assert result["analysis"].dropped_articles[0].reason == "ignored_geo_without_target_confirmation"
    candidates_payload = json.loads(result["candidates_path"].read_text(encoding="utf-8"))
    assert candidates_payload == []
    dropped_payload = json.loads(result["dropped_articles_path"].read_text(encoding="utf-8"))
    assert dropped_payload[0]["reason"] == "ignored_geo_without_target_confirmation"
    assert dropped_payload[0]["details"]["ignored_geo_terms"] == "USA | United States"
    summary_payload = json.loads(result["summary_path"].read_text(encoding="utf-8"))
    assert summary_payload["drop_reasons"] == {"ignored_geo_without_target_confirmation": 1}


def test_run_pipeline_filters_guardian_grab_false_positive_without_brand_context(
    tmp_path, monkeypatch
):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)
    providers = [
        StaticProvider(
            "guardian",
            [
                RawArticle(
                    title="High stakes: who’s leading the fight against Labor’s CGT reform – and what’s in it for them?",
                    url="https://example.com/cgt-reform",
                    provider="guardian",
                    source="The Guardian",
                    published_at="2026-05-19T09:00:00Z",
                    snippet=(
                        "Tax critics turn to AI memes and airport billboards in addition to traditional lobbying tactics."
                    ),
                    query='"Grab" market expansion Southeast Asia',
                    region=None,
                    language="en",
                    competitor_hints=("Grab",),
                    metadata={"source_tier": "tier2_direct"},
                )
            ],
        )
    ]
    monkeypatch.setattr(cli, "build_providers", lambda names: providers)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert result["digest"].alerts == ()
    assert result["analysis"].candidates == []
    assert result["analysis"].dropped_count == 1
    assert result["analysis"].dropped_articles[0].reason == "no_competitor_match"


def test_already_sent_suppression_filters_next_run(tmp_path, monkeypatch):
    config = build_config()
    runtime = patch_runtime(monkeypatch, tmp_path, config)
    shared_articles = [
        article(
            competitor="Grab",
            title="Grab launches driver support program in Manila",
            url="https://example.com/grab-driver-support-manila",
        )
    ]
    monkeypatch.setattr(cli, "build_providers", lambda names: [StaticProvider("mock_news", shared_articles)])

    first = cli.run_pipeline(days=7, min_score=5, regions=["sea"])
    assert len(first["digest"].alerts) == 1

    storage = SQLiteTrackerStorage(runtime.database_path)
    storage.mark_delivered(
        alert_key=first["digest"].alerts[0].digest_key,
        channel="daily_digest",
        delivered_at="2026-05-20T09:00:00Z",
        destination="",
    )

    second = cli.run_pipeline(days=7, min_score=5, regions=["sea"])
    assert len(second["digest"].alerts) == 0


def test_top_digest_cap_limits_integration_output_to_ten(tmp_path, monkeypatch):
    freeze_cli_today(monkeypatch)
    articles = build_capped_articles(12)
    config = build_config(
        daily_digest_limit=10,
        competitors_by_region={"sea": [item.competitor_hints[0] for item in articles]},
    )
    patch_runtime(monkeypatch, tmp_path, config)
    for index, item in enumerate(articles):
        articles[index] = RawArticle(
            title=item.title,
            url=item.url,
            provider=item.provider,
            source=item.source,
            published_at=f"2026-05-{19 - (index % 3):02d}T09:00:00Z",
            snippet=item.snippet,
            query=item.query,
            region=item.region,
            language=item.language,
            competitor_hints=item.competitor_hints,
            metadata=item.metadata,
        )
    monkeypatch.setattr(cli, "build_providers", lambda names: [StaticProvider("mock_news", articles)])

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert len(result["digest"].alerts) == 10


def test_provider_partial_failure_keeps_run_alive_and_records_summary(tmp_path, monkeypatch):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)
    providers = [
        StaticProvider(
            "mock_news",
            [
                article(
                    competitor="Grab",
                    title="Grab launches partnership in Cebu",
                    url="https://example.com/grab-cebu-partnership",
                )
            ],
        ),
        FailingProvider(),
    ]
    monkeypatch.setattr(cli, "build_providers", lambda names: providers)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert len(result["digest"].alerts) == 1
    summary_payload = json.loads(result["summary_path"].read_text(encoding="utf-8"))
    assert summary_payload["provider_errors"] == {"failing_provider": "temporary upstream failure"}
    assert summary_payload["provider_diagnostics"]["failing_provider"]["status"] == "error"
    assert summary_payload["provider_diagnostics"]["failing_provider"]["queries"][0]["exception"] == (
        "temporary upstream failure"
    )

    with sqlite3.connect(tmp_path / "output" / "tracker.db") as connection:
        row = connection.execute(
            """
            SELECT
                status,
                provider_errors_json,
                provider_diagnostics_json
            FROM runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert row[0:2] == (
        "success_with_provider_errors",
        '{"failing_provider": "temporary upstream failure"}',
    )
    assert '"failing_provider"' in row[2]


def test_run_pipeline_logs_sqlite_run_summary_failure_without_crashing(
    tmp_path, monkeypatch, caplog
):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [
            StaticProvider(
                "mock_news",
                [
                    article(
                        competitor="Grab",
                        title="Grab launches new city campaign in Manila",
                        url="https://example.com/grab-manila-storage-warning",
                        snippet="Grab launches a new city campaign in Manila with driver messaging.",
                    )
                ],
            )
        ],
    )

    original_insert_run = cli.SQLiteTrackerStorage.insert_run

    def failing_insert_run(self, summary):
        raise sqlite3.OperationalError("runs table is locked")

    monkeypatch.setattr(cli.SQLiteTrackerStorage, "insert_run", failing_insert_run)

    result = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
    )

    assert len(result["digest"].alerts) == 1
    assert result["summary_path"].exists()
    assert "Failed to persist run summary to SQLite runs table" in caplog.text

    monkeypatch.setattr(cli.SQLiteTrackerStorage, "insert_run", original_insert_run)


def test_unknown_enabled_provider_is_visible_in_logs_and_run_summary(
    tmp_path, monkeypatch, caplog
):
    config = build_config()
    config = TrackerConfig.from_dict(
        {
            "regions": {
                region: {
                    "label": details.label,
                    "geo_terms": list(details.geo_terms),
                    "country_validation_terms": list(details.country_validation_terms),
                    "language_hints": list(details.language_hints),
                }
                for region, details in config.regions.items()
            },
            "competitors_by_region": {
                region: list(competitors)
                for region, competitors in config.competitors_by_region.items()
            },
            "topic_groups": {
                topic: list(keywords)
                for topic, keywords in config.topic_groups.items()
            },
            "keyword_templates": list(config.keyword_templates),
            "ignored_geo_terms": list(config.ignored_geo_terms),
            "daily_digest_limit": config.daily_digest_limit,
            "enabled_providers": ["mystery_provider"],
        }
    )
    patch_runtime(monkeypatch, tmp_path, config)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert result["raw_articles_count"] == 0
    assert "Unknown provider 'mystery_provider' is enabled in config" in caplog.text
    summary_payload = json.loads(result["summary_path"].read_text(encoding="utf-8"))
    assert summary_payload["providers"] == ["mystery_provider"]
    assert summary_payload["provider_errors"] == {
        "mystery_provider": (
            "Provider 'mystery_provider' is enabled in config but is not supported by competitor_tracker"
        )
    }


@pytest.mark.parametrize(
    ("llm_payload", "expected_country", "expected_country_source", "expected_fallback"),
    [
        (
            {
                "competitor": "Uber",
                "region": "sea",
                "country": "Philippines",
            },
            "Philippines",
            "pipeline",
            True,
        ),
        (
            {
                "competitor": "Grab",
                "region": "latam",
                "country": "Philippines",
            },
            "Philippines",
            "pipeline",
            True,
        ),
        (
            {
                "competitor": "Grab",
                "region": "sea",
                "country": "Mexico",
            },
            "Philippines",
            "pipeline",
            True,
        ),
    ],
)
def test_run_pipeline_falls_back_to_candidate_truth_layer_on_bad_llm_geo_output(
    tmp_path,
    monkeypatch,
    llm_payload,
    expected_country,
    expected_country_source,
    expected_fallback,
):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config, use_llm_alerts=True, llm_top_n=15)
    source_article = article(
        competitor="Grab",
        title="Grab launches driver campaign in the Philippines",
        url="https://example.com/grab-philippines-campaign",
        snippet="Grab is expanding driver messaging in the Philippines.",
        query='"Grab" campaign_launches Southeast Asia',
        region="sea",
    )
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [StaticProvider("mock_news", [source_article])],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class FakeContextExtractor:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, candidate):
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body="Grab launched a driver campaign in the Philippines.",
                published_at="2026-05-20",
            )

        def build_fallback_context(self, candidate):
            return self.extract(candidate)

    class FakeOpenAIClient:
        def __init__(self, api_key=None, http_client=None):
            self.chat = type(
                "ChatNamespace",
                (),
                {
                    "completions": type(
                        "CompletionNamespace",
                        (),
                        {
                            "create": staticmethod(
                                lambda **kwargs: type(
                                    "Response",
                                    (),
                                    {
                                        "choices": [
                                            type(
                                                "Choice",
                                                (),
                                                {
                                                    "message": type(
                                                        "Message",
                                                        (),
                                                        {
                                                            "content": json.dumps(
                                                                {
                                                                    **llm_payload,
                                                                    "topic": "campaign launches",
                                                                    "priority": "MEDIUM",
                                                                    "published_date": "2026-05-20",
                                                                    "published_date_source": "llm",
                                                                    "what_happened": "LLM returned geo fields.",
                                                                    "why_it_matters": "Geo truth layer should validate this.",
                                                                    "potential_impact": "Potential messaging impact.",
                                                                    "recommended_action": "Keep pipeline truth.",
                                                                    "confidence": 0.82,
                                                                }
                                                            )
                                                        },
                                                    )()
                                                },
                                            )()
                                        ]
                                    },
                                )()
                            )
                        },
                    )()
                },
            )()

    monkeypatch.setattr(cli, "ArticleContextExtractor", FakeContextExtractor)
    monkeypatch.setattr("competitor_tracker.analyzer.openai.OpenAI", FakeOpenAIClient)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert len(result["digest"].alerts) == 1
    assert len(result["alert_schemas"]) == 1

    alert = result["alert_schemas"][0]
    candidate = result["digest"].alerts[0].candidate

    assert candidate.competitor == "Grab"
    assert candidate.region == "sea"
    assert candidate.country_hint == "Philippines"

    assert alert["competitor"] == candidate.competitor
    assert alert["region"] == "SEA"
    assert alert["country"] == expected_country
    assert alert["competitor_source"] == "pipeline"
    assert alert["region_source"] == "pipeline"
    assert alert["country_source"] == expected_country_source
    assert alert["geo_validation_fallback"] is expected_fallback


def test_runtime_llm_settings_control_enrichment_and_context_limit(tmp_path, monkeypatch):
    articles = build_capped_articles(3)
    config = build_config(
        daily_digest_limit=3,
        competitors_by_region={"sea": [item.competitor_hints[0] for item in articles]},
    )
    patch_runtime(
        monkeypatch,
        tmp_path,
        config,
        use_llm_alerts=True,
        llm_top_n=1,
        article_context_max_chars=25,
    )
    monkeypatch.setattr(cli, "build_providers", lambda names: [StaticProvider("mock_news", articles)])

    extractor_inits = []
    analyzer_modes = []

    class FakeContextExtractor:
        def __init__(self, *args, max_chars=8000, **kwargs):
            extractor_inits.append(max_chars)

        def extract(self, candidate):
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body="x" * 25,
                published_at="2026-05-20",
                published_at_source="html_scraped",
            )

        def build_fallback_context(self, candidate):
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body="",
                published_at=None,
                published_at_source=None,
            )

    class FakeAlertAnalyzer:
        def __init__(self, use_llm, model=None, config=None):
            analyzer_modes.append(use_llm)
            self.use_llm = use_llm

        def analyze_candidate(self, candidate, *, article_context=None):
            return {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group,
                "priority": "MEDIUM",
                "what_happened": article_context.article_body if article_context else "fallback",
                "why_it_matters": "llm" if self.use_llm else "fallback",
                "potential_impact": "impact",
                "recommended_action": "act",
                "confidence": 0.7,
            }

    monkeypatch.setattr(cli, "ArticleContextExtractor", FakeContextExtractor)
    monkeypatch.setattr(cli, "CompetitorAlertAnalyzer", FakeAlertAnalyzer)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert extractor_inits == [25, 25]
    assert analyzer_modes == [False, True]
    assert result["alert_schemas"][0]["why_it_matters"] == "llm"
    assert result["alert_schemas"][1]["why_it_matters"] == "fallback"


def test_run_pipeline_llm_enriches_only_top_n_with_extended_article_context(
    tmp_path, monkeypatch
):
    config = build_config(
        daily_digest_limit=2,
        competitors_by_region={"sea": ["Grab", "Gojek"]},
    )
    patch_runtime(monkeypatch, tmp_path, config, use_llm_alerts=True, llm_top_n=1)
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [
            StaticProvider(
                "mock_news",
                [
                    article(
                        competitor="Grab",
                        title="Grab launches strategic partnership campaign in Manila",
                        url="https://example.com/grab-strategic-partnership",
                        query='"Grab" campaign_launches Southeast Asia',
                        snippet="Grab launches a strategic partnership and new feature campaign in Manila.",
                    ),
                    article(
                        competitor="Gojek",
                        title="Gojek launches campaign in Cebu",
                        url="https://example.com/gojek-campaign-cebu",
                        query='"Gojek" campaign_launches Southeast Asia',
                        snippet="Gojek launches a campaign in Cebu.",
                    ),
                ],
            )
        ],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    captured = {"calls": 0, "prompt": ""}

    class FakeContextExtractor:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, candidate):
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body=(
                    "Extended article body: Grab frames the move as a strategic partnership, "
                    "driver narrative, and rider trust push in Manila."
                ),
                published_at="2026-05-20",
                published_at_source="html_scraped",
            )

        def build_fallback_context(self, candidate):
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body="",
                published_at=None,
                published_at_source=None,
            )

    class FakeOpenAIClient:
        def __init__(self, api_key=None, http_client=None):
            self.chat = type(
                "ChatNamespace",
                (),
                {
                    "completions": type(
                        "CompletionNamespace",
                        (),
                        {
                            "create": staticmethod(
                                lambda **kwargs: _build_llm_response(
                                    kwargs,
                                    captured,
                                    {
                                        "competitor": "Grab",
                                        "region": "sea",
                                        "country": "Philippines",
                                        "topic": "campaign launches",
                                        "priority": "HIGH",
                                        "published_date": "2026-05-20",
                                        "published_date_source": "llm",
                                        "what_happened": "LLM summary from extended body.",
                                        "why_it_matters": "LLM used the extended article body.",
                                        "potential_impact": "Potential trust and messaging impact.",
                                        "recommended_action": "Respond with localized Marcom messaging.",
                                        "confidence": 0.91,
                                    },
                                )
                            )
                        },
                    )()
                },
            )()

    def _build_llm_response(kwargs, target, payload):
        target["calls"] += 1
        target["prompt"] = kwargs["messages"][1]["content"]
        return type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type(
                                "Message",
                                (),
                                {
                                    "content": json.dumps(payload),
                                },
                            )()
                        },
                    )()
                ]
            },
        )()

    monkeypatch.setattr(cli, "ArticleContextExtractor", FakeContextExtractor)
    monkeypatch.setattr("competitor_tracker.analyzer.openai.OpenAI", FakeOpenAIClient)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert captured["calls"] == 1
    assert "Extended article body:" in captured["prompt"]
    assert "strategic partnership, driver narrative, and rider trust push" in captured["prompt"]
    assert "https://example.com/grab-strategic-partnership" in captured["prompt"]
    assert len(result["alert_schemas"]) == 2
    assert result["article_contexts"][0].article_body.startswith("Extended article body:")
    assert result["article_contexts"][1].article_body == ""
    assert result["alert_schemas"][0]["what_happened"] == "LLM summary from extended body."
    assert result["alert_schemas"][0]["why_it_matters"] == "LLM used the extended article body."
    assert result["alert_schemas"][0]["recommended_action"] == "Respond with localized Marcom messaging."
    assert result["alert_schemas"][1]["why_it_matters"] != "LLM used the extended article body."


def test_run_pipeline_invalid_llm_json_falls_back_to_rule_based_alert(
    tmp_path, monkeypatch
):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config, use_llm_alerts=True, llm_top_n=1)
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [
            StaticProvider(
                "mock_news",
                [
                    article(
                        competitor="Grab",
                        title="Grab launches driver campaign in Manila",
                        url="https://example.com/grab-invalid-json",
                        query='"Grab" campaign_launches Southeast Asia',
                    )
                ],
            )
        ],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    llm_calls = {"count": 0}

    class FakeContextExtractor:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, candidate):
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body="Grab launched a driver campaign in Manila.",
                published_at="2026-05-20",
                published_at_source="html_scraped",
            )

        def build_fallback_context(self, candidate):
            return self.extract(candidate)

    class FakeOpenAIClient:
        def __init__(self, api_key=None, http_client=None):
            self.chat = type(
                "ChatNamespace",
                (),
                {
                    "completions": type(
                        "CompletionNamespace",
                        (),
                        {
                            "create": staticmethod(
                                lambda **kwargs: _invalid_json_response(llm_calls)
                            )
                        },
                    )()
                },
            )()

    def _invalid_json_response(target):
        target["count"] += 1
        return type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type(
                                "Message",
                                (),
                                {
                                    "content": "this is not valid json",
                                },
                            )()
                        },
                    )()
                ]
            },
        )()

    monkeypatch.setattr(cli, "ArticleContextExtractor", FakeContextExtractor)
    monkeypatch.setattr("competitor_tracker.analyzer.openai.OpenAI", FakeOpenAIClient)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert llm_calls["count"] == 1
    assert len(result["alert_schemas"]) == 1
    alert = result["alert_schemas"][0]
    assert alert["what_happened"].startswith("Grab appears in coverage related to")
    assert "This may indicate a competitor move" in alert["why_it_matters"]
    assert alert["recommended_action"].startswith("Review the signal, validate local market context")
    assert alert["published_date_source"] == "html_scraped"


def test_run_pipeline_no_body_context_uses_insufficient_source_fallback(
    tmp_path, monkeypatch
):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config, use_llm_alerts=True, llm_top_n=1)
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [
            StaticProvider(
                "mock_news",
                [
                    article(
                        competitor="Grab",
                        title="Grab launches driver campaign in Manila",
                        url="https://example.com/grab-no-body",
                        query='"Grab" campaign_launches Southeast Asia',
                    )
                ],
            )
        ],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    llm_calls = {"count": 0}

    class FakeContextExtractor:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, candidate):
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body="",
                published_at="2026-05-20",
                published_at_source="html_scraped",
            )

        def build_fallback_context(self, candidate):
            return self.extract(candidate)

    class FakeOpenAIClient:
        def __init__(self, api_key=None, http_client=None):
            self.chat = type(
                "ChatNamespace",
                (),
                {
                    "completions": type(
                        "CompletionNamespace",
                        (),
                        {
                            "create": staticmethod(
                                lambda **kwargs: _unexpected_llm_call(llm_calls)
                            )
                        },
                    )()
                },
            )()

    def _unexpected_llm_call(target):
        target["count"] += 1
        raise AssertionError("LLM should not be called when article body is empty")

    monkeypatch.setattr(cli, "ArticleContextExtractor", FakeContextExtractor)
    monkeypatch.setattr("competitor_tracker.analyzer.openai.OpenAI", FakeOpenAIClient)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert llm_calls["count"] == 0
    assert len(result["alert_schemas"]) == 1
    alert = result["alert_schemas"][0]
    assert alert["why_it_matters"] == CompetitorAlertAnalyzer.INSUFFICIENT_SOURCE_DATA_MESSAGE
    assert alert["recommended_action"] == CompetitorAlertAnalyzer.INSUFFICIENT_SOURCE_DATA_MESSAGE
    assert alert["confidence"] == 0.0
    assert alert["published_date"] == "2026-05-20"


def test_telegram_delivery_uses_enriched_alert_cards_from_llm_output(
    tmp_path, monkeypatch
):
    config = build_config()
    patch_runtime(
        monkeypatch,
        tmp_path,
        config,
        use_llm_alerts=True,
        llm_top_n=1,
        telegram_top_n=1,
    )
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [
            StaticProvider(
                "mock_news",
                [
                    article(
                        competitor="Grab",
                        title="Grab launches partnership campaign in Manila",
                        url="https://example.com/grab-telegram-enriched",
                        query='"Grab" campaign_launches Southeast Asia',
                    )
                ],
            )
        ],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class FakeContextExtractor:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, candidate):
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body="Rich body for Telegram formatting.",
                published_at="2026-05-20",
                published_at_source="html_scraped",
            )

        def build_fallback_context(self, candidate):
            return self.extract(candidate)

    class FakeOpenAIClient:
        def __init__(self, api_key=None, http_client=None):
            self.chat = type(
                "ChatNamespace",
                (),
                {
                    "completions": type(
                        "CompletionNamespace",
                        (),
                        {
                            "create": staticmethod(
                                lambda **kwargs: type(
                                    "Response",
                                    (),
                                    {
                                        "choices": [
                                            type(
                                                "Choice",
                                                (),
                                                {
                                                    "message": type(
                                                        "Message",
                                                        (),
                                                        {
                                                            "content": json.dumps(
                                                                {
                                                                    "competitor": "Grab",
                                                                    "region": "sea",
                                                                    "country": "Philippines",
                                                                    "topic": "campaign launches",
                                                                    "priority": "HIGH",
                                                                    "published_date": "2026-05-20",
                                                                    "published_date_source": "llm",
                                                                    "what_happened": "LLM telegram event summary.",
                                                                    "why_it_matters": "LLM telegram strategic angle.",
                                                                    "potential_impact": "LLM telegram impact.",
                                                                    "recommended_action": "LLM telegram action.",
                                                                    "confidence": 0.88,
                                                                }
                                                            )
                                                        },
                                                    )()
                                                },
                                            )()
                                        ]
                                    },
                                )()
                            )
                        },
                    )()
                },
            )()

    telegram_capture = {}

    class FakeTelegramSender:
        def __init__(self, storage, dry_run):
            telegram_capture["dry_run"] = dry_run

        def send_daily_digest(self, alert_schemas, alerts, source_urls, generated_at):
            telegram_capture["schemas"] = list(alert_schemas)
            telegram_capture["text"] = format_daily_digest(
                alert_schemas,
                source_urls=source_urls,
                generated_at=generated_at,
            )
            return {"ok": True, "dry_run": True}

    monkeypatch.setattr(cli, "ArticleContextExtractor", FakeContextExtractor)
    monkeypatch.setattr("competitor_tracker.analyzer.openai.OpenAI", FakeOpenAIClient)
    monkeypatch.setattr(cli, "TelegramSender", FakeTelegramSender)

    result = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="dry",
    )

    assert len(result["alert_schemas"]) == 1
    assert telegram_capture["dry_run"] is True
    assert telegram_capture["schemas"][0]["why_it_matters"] == "LLM telegram strategic angle."
    assert "LLM telegram event summary." in telegram_capture["text"]
    assert "LLM telegram strategic angle." in telegram_capture["text"]
    assert "LLM telegram action." in telegram_capture["text"]


def test_run_pipeline_does_not_call_llm_for_low_priority_noise(
    tmp_path, monkeypatch
):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config, use_llm_alerts=True, llm_top_n=1)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    raw_articles = [
        article(
            competitor="Grab",
            title="Grab strategic partnership campaign in Manila",
            url="https://example.com/grab-high-priority",
            query='"Grab" campaign_launches Southeast Asia',
        ),
        article(
            competitor="Gojek",
            title="Gojek campaign note",
            url="https://example.com/gojek-low-priority",
            query='"Gojek" campaign_launches Southeast Asia',
        ),
    ]

    def fake_collect_raw_articles(**kwargs):
        return raw_articles, ("mock_news",), {}

    def fake_prefilter(self, raw_articles, regions=None):
        return AnalysisResult(
            candidates=[
                CandidateArticle(
                    raw_article=raw_articles[0],
                    competitor="Grab",
                    topic_group="campaign_launches",
                    score=9,
                    matched_keywords=("campaign", "partnership"),
                    summary="Grab strategic partnership campaign in Manila",
                    region="sea",
                    country_hint="Philippines",
                    language_hint="en",
                    reasons=("competitor_mentioned", "priority_signal"),
                ),
                CandidateArticle(
                    raw_article=raw_articles[1],
                    competitor="Gojek",
                    topic_group="campaign_launches",
                    score=5,
                    matched_keywords=("campaign",),
                    summary="Gojek campaign note",
                    region="sea",
                    country_hint=None,
                    language_hint="en",
                    reasons=("competitor_mentioned",),
                ),
            ],
            dropped_count=0,
            dropped_articles=[],
        )

    llm_calls = {"count": 0}

    class FakeContextExtractor:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, candidate):
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body="High-priority article body for LLM.",
                published_at="2026-05-20",
                published_at_source="html_scraped",
            )

        def build_fallback_context(self, candidate):
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body="",
                published_at=None,
                published_at_source=None,
            )

    class FakeOpenAIClient:
        def __init__(self, api_key=None, http_client=None):
            self.chat = type(
                "ChatNamespace",
                (),
                {
                    "completions": type(
                        "CompletionNamespace",
                        (),
                        {
                            "create": staticmethod(
                                lambda **kwargs: _low_noise_llm_response(llm_calls)
                            )
                        },
                    )()
                },
            )()

    def _low_noise_llm_response(target):
        target["count"] += 1
        return type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type(
                                "Message",
                                (),
                                {
                                    "content": json.dumps(
                                        {
                                            "competitor": "Grab",
                                            "region": "sea",
                                            "country": "Philippines",
                                            "topic": "campaign launches",
                                            "priority": "HIGH",
                                            "published_date": "2026-05-20",
                                            "published_date_source": "llm",
                                            "what_happened": "High-priority LLM summary.",
                                            "why_it_matters": "High-priority LLM significance.",
                                            "potential_impact": "High-priority impact.",
                                            "recommended_action": "High-priority action.",
                                            "confidence": 0.9,
                                        }
                                    )
                                },
                            )()
                        },
                    )()
                ]
            },
        )()

    monkeypatch.setattr(cli, "collect_raw_articles", fake_collect_raw_articles)
    monkeypatch.setattr(cli.CompetitorAnalyzer, "prefilter_raw_articles", fake_prefilter)
    monkeypatch.setattr(cli, "ArticleContextExtractor", FakeContextExtractor)
    monkeypatch.setattr("competitor_tracker.analyzer.openai.OpenAI", FakeOpenAIClient)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert llm_calls["count"] == 1
    assert len(result["alert_schemas"]) == 2
    assert result["alert_schemas"][0]["priority"] == "HIGH"
    assert result["alert_schemas"][0]["why_it_matters"] == "High-priority LLM significance."
    assert result["alert_schemas"][1]["priority"] == "LOW"
    assert result["alert_schemas"][1]["why_it_matters"] != "High-priority LLM significance."


def test_optional_notion_behavior_skips_cleanly_when_env_missing(tmp_path, monkeypatch):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [
            StaticProvider(
                "mock_news",
                [
                    article(
                        competitor="Grab",
                        title="Grab launches promo in Manila",
                        url="https://example.com/grab-promo-manila",
                    )
                ],
            )
        ],
    )
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("COMPETITOR_TRACKER_NOTION_DATABASE_ID", raising=False)
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"], notion_mode="sync")

    assert result["notion_result"] == {
        "created": 0,
        "updated": 0,
        "skipped": 1,
        "would_create": 0,
        "would_update": 0,
    }


def test_top_fifteen_cap_can_be_supported_via_config(tmp_path, monkeypatch):
    articles = build_capped_articles(16)
    config = build_config(
        daily_digest_limit=15,
        competitors_by_region={"sea": [item.competitor_hints[0] for item in articles]},
    )
    patch_runtime(monkeypatch, tmp_path, config)
    monkeypatch.setattr(cli, "build_providers", lambda names: [StaticProvider("mock_news", articles)])

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert len(result["digest"].alerts) == 15


def test_post_ranking_llm_enrichment_only_applies_to_top_fifteen(tmp_path, monkeypatch):
    articles = build_capped_articles(16)
    config = build_config(
        daily_digest_limit=16,
        competitors_by_region={"sea": [item.competitor_hints[0] for item in articles]},
    )
    patch_runtime(monkeypatch, tmp_path, config, use_llm_alerts=True, llm_top_n=15)
    monkeypatch.setattr(cli, "build_providers", lambda names: [StaticProvider("mock_news", articles)])

    analyzer_inits = []
    llm_calls = []
    fallback_calls = []

    class FakeAlertAnalyzer:
        def __init__(self, use_llm, model=None):
            analyzer_inits.append(use_llm)
            self.use_llm = use_llm

        def analyze_candidate(self, candidate, *, article_context=None):
            payload = {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group,
                "priority": "MEDIUM",
                "what_happened": candidate.title,
                "why_it_matters": "llm" if self.use_llm else "fallback",
                "potential_impact": "impact",
                "recommended_action": "act",
                "confidence": 0.7,
            }
            if self.use_llm:
                llm_calls.append(candidate.competitor)
            else:
                fallback_calls.append(candidate.competitor)
            return payload

    monkeypatch.setattr(cli, "CompetitorAlertAnalyzer", FakeAlertAnalyzer)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert len(result["digest"].alerts) == 16
    assert analyzer_inits == [False, True]
    assert len(llm_calls) == 15
    assert len(fallback_calls) == 1
    assert result["alert_schemas"][-1]["why_it_matters"] == "fallback"


def test_post_ranking_llm_enrichment_applies_per_region(tmp_path, monkeypatch):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "sea": {
                    "label": "Southeast Asia",
                    "geo_terms": ["Philippines", "Indonesia", "Thailand"],
                    "country_validation_terms": ["Philippines", "Indonesia", "Thailand"],
                    "language_hints": ["en"],
                },
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico", "Brazil", "Colombia"],
                    "country_validation_terms": ["Mexico", "Brazil", "Colombia"],
                    "language_hints": ["en", "es", "pt"],
                },
            },
            "competitors_by_region": {
                "sea": ["Grab", "Gojek"],
                "latam": ["Rappi", "DiDi"],
            },
            "topic_groups": {
                "market_entry": ["launch", "new city", "entering market"],
            },
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "ignored_geo_terms": ["USA", "United States", "Europe"],
            "daily_digest_limit": 2,
            "enabled_providers": ["google_news_rss"],
        }
    )
    patch_runtime(
        monkeypatch,
        tmp_path,
        config,
        use_llm_alerts=True,
        llm_top_n=1,
    )
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [
            StaticProvider(
                "mock_news",
                [
                    article(
                        competitor="Grab",
                        title="Grab launches ferry permit campaign in Manila",
                        url="https://example.com/grab-manila",
                        region="sea",
                        query='"Grab" market_entry Southeast Asia',
                    ),
                    article(
                        competitor="Gojek",
                        title="Gojek launches driver hub campaign in Jakarta",
                        url="https://example.com/gojek-jakarta",
                        region="sea",
                        query='"Gojek" market_entry Southeast Asia',
                    ),
                    article(
                        competitor="Rappi",
                        title="Rappi launches grocery courier campaign in Mexico City",
                        url="https://example.com/rappi-mexico-city",
                        region="latam",
                        query='"Rappi" market_entry Latin America',
                    ),
                    article(
                        competitor="DiDi",
                        title="DiDi launches women driver campaign in Sao Paulo",
                        url="https://example.com/didi-sao-paulo",
                        region="latam",
                        query='"DiDi" market_entry Latin America',
                    ),
                ],
            )
        ],
    )

    class FakeAlertAnalyzer:
        def __init__(self, use_llm, model=None):
            self.use_llm = use_llm

        def analyze_candidate(self, candidate, *, article_context=None):
            return {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group,
                "priority": "MEDIUM",
                "what_happened": candidate.title,
                "why_it_matters": "llm" if self.use_llm else "fallback",
                "potential_impact": "impact",
                "recommended_action": "act",
                "confidence": 0.7,
            }

    monkeypatch.setattr(cli, "CompetitorAlertAnalyzer", FakeAlertAnalyzer)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea", "latam"])

    assert len(result["digest"].alerts) == 4
    sea_schemas = [item for item in result["alert_schemas"] if item["region"] == "sea"]
    latam_schemas = [item for item in result["alert_schemas"] if item["region"] == "latam"]
    assert len(sea_schemas) == 2
    assert len(latam_schemas) == 2
    assert sum(1 for item in sea_schemas if item["why_it_matters"] == "llm") == 1
    assert sum(1 for item in latam_schemas if item["why_it_matters"] == "llm") == 1
    assert sum(1 for item in result["alert_schemas"] if item["why_it_matters"] == "llm") == 2


def test_post_ranking_llm_top_n_is_independent_for_latam_and_cis(tmp_path, monkeypatch):
    latam_competitors = [f"LatamComp{index}" for index in range(10)]
    cis_competitors = [f"CisComp{index}" for index in range(3)]
    latam_descriptors = [
        "airport shuttle rewards",
        "grocery courier insurance",
        "women driver academy",
        "night bus transfer pass",
        "electric bike rental launch",
        "school pickup partnership",
        "cargo van pilot",
        "tourist transfer bundle",
        "commuter wallet cashback",
        "stadium event routing",
    ]
    latam_cities = [
        "Mexico City",
        "Sao Paulo",
        "Bogota",
        "Guadalajara",
        "Monterrey",
        "Medellin",
        "Brasilia",
        "Lima",
        "Curitiba",
        "Recife",
    ]
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico", "Brazil", "Colombia"],
                    "country_validation_terms": ["Mexico", "Brazil", "Colombia"],
                    "language_hints": ["en", "es", "pt"],
                },
                "cis": {
                    "label": "CIS",
                    "geo_terms": ["Kazakhstan", "Uzbekistan", "Armenia"],
                    "country_validation_terms": ["Kazakhstan", "Uzbekistan", "Armenia"],
                    "language_hints": ["en", "ru"],
                },
            },
            "competitors_by_region": {
                "latam": latam_competitors,
                "cis": cis_competitors,
            },
            "topic_groups": {
                "market_entry": ["launch", "new city", "entering market"],
            },
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "ignored_geo_terms": ["USA", "United States", "Europe"],
            "daily_digest_limit": 10,
            "enabled_providers": ["google_news_rss"],
        }
    )
    patch_runtime(
        monkeypatch,
        tmp_path,
        config,
        use_llm_alerts=True,
        llm_top_n=5,
    )

    latam_articles = [
        article(
            competitor=competitor,
            title=f"{competitor} launches {latam_descriptors[index]} in {latam_cities[index]}",
            url=f"https://example.com/latam-{index}",
            region="latam",
            query=f'"{competitor}" market_entry Latin America',
        )
        for index, competitor in enumerate(latam_competitors)
    ]
    cis_titles = [
        "launches courier pilot in Almaty",
        "launches pharmacy delivery hub in Tashkent",
        "launches airport transfer program in Yerevan",
    ]
    cis_articles = [
        article(
            competitor=competitor,
            title=f"{competitor} {cis_titles[index]}",
            url=f"https://example.com/cis-{index}",
            region="cis",
            query=f'"{competitor}" market_entry CIS',
        )
        for index, competitor in enumerate(cis_competitors)
    ]
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [StaticProvider("mock_news", [*latam_articles, *cis_articles])],
    )

    class FakeAlertAnalyzer:
        def __init__(self, use_llm, model=None, config=None):
            self.use_llm = use_llm

        def analyze_candidate(self, candidate, *, article_context=None):
            return {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group,
                "priority": "MEDIUM",
                "what_happened": candidate.title,
                "why_it_matters": "llm" if self.use_llm else "fallback",
                "potential_impact": "impact",
                "recommended_action": "act",
                "confidence": 0.7,
            }

    monkeypatch.setattr(cli, "CompetitorAlertAnalyzer", FakeAlertAnalyzer)

    result = cli.run_pipeline(days=7, min_score=5, regions=["latam", "cis"])

    assert len(result["digest"].alerts) == 13

    latam_schemas = [item for item in result["alert_schemas"] if item["region"] == "latam"]
    cis_schemas = [item for item in result["alert_schemas"] if item["region"] == "cis"]
    assert len(latam_schemas) == 10
    assert len(cis_schemas) == 3
    assert sum(1 for item in latam_schemas if item["why_it_matters"] == "llm") == 5
    assert sum(1 for item in latam_schemas if item["why_it_matters"] == "fallback") == 5
    assert sum(1 for item in cis_schemas if item["why_it_matters"] == "llm") == 3
    assert sum(1 for item in cis_schemas if item["why_it_matters"] == "fallback") == 0

    summary_payload = json.loads(result["summary_path"].read_text(encoding="utf-8"))
    assert summary_payload["regions"] == ["latam", "cis"]
    assert summary_payload["raw_articles_collected"] == 13
    assert summary_payload["alerts_created"] == 13
    assert summary_payload["drop_reasons"] == {}

    dropped_payload = json.loads(result["dropped_articles_path"].read_text(encoding="utf-8"))
    assert dropped_payload == []


def test_multi_region_collection_keeps_query_specific_competitor_hints(tmp_path, monkeypatch):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico"],
                    "country_validation_terms": ["Mexico"],
                    "language_hints": ["es", "en"],
                },
                "cis": {
                    "label": "CIS",
                    "geo_terms": ["Kazakhstan"],
                    "country_validation_terms": ["Kazakhstan"],
                    "language_hints": ["ru", "en"],
                },
            },
            "competitors_by_region": {
                "latam": ["Uber", "DiDi"],
                "cis": ["Yandex Go"],
            },
            "topic_groups": {
                "market_entry": ["launch"],
            },
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "ignored_geo_terms": ["USA"],
            "daily_digest_limit": 10,
            "enabled_providers": ["google_news_rss"],
        }
    )
    patch_runtime(monkeypatch, tmp_path, config)

    class QueryAwareProvider:
        name = "query_aware_provider"

        def fetch(self, request):
            articles = []
            for query in request.queries:
                competitor = request.competitor_hints_for_query(query)[0]
                region = "latam" if competitor in {"Uber", "DiDi"} else "cis"
                city = "Mexico City" if competitor == "Uber" else (
                    "Guadalajara" if competitor == "DiDi" else "Almaty"
                )
                descriptor = {
                    "Uber": "launch airport transfer expansion",
                    "DiDi": "launch driver rewards rollout",
                    "Yandex Go": "launch courier pilot",
                }[competitor]
                articles.append(
                    RawArticle(
                        title=f"{competitor} {descriptor} in {city}",
                        url=f"https://example.com/{competitor.casefold().replace(' ', '-')}",
                        provider=self.name,
                        source="Example News",
                        published_at="2026-05-19T09:00:00Z",
                        snippet=f"{competitor} {descriptor} in {city}",
                        query=query,
                        region=region,
                        language="en",
                        competitor_hints=request.competitor_hints_for_query(query),
                    )
                )
            return articles

    monkeypatch.setattr(cli, "build_providers", lambda names: [QueryAwareProvider()])

    result = cli.run_pipeline(days=7, min_score=5, regions=["latam", "cis"])

    assert len(result["analysis"].candidates) == 3
    assert {candidate.competitor for candidate in result["analysis"].candidates} == {
        "Uber",
        "DiDi",
        "Yandex Go",
    }
    assert {candidate.region for candidate in result["analysis"].candidates} == {
        "latam",
        "cis",
    }


def test_run_pipeline_keeps_global_uber_news_in_query_owner_region(tmp_path, monkeypatch):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "sea": {
                    "label": "Southeast Asia",
                    "geo_terms": ["Philippines", "Indonesia"],
                    "country_validation_terms": ["Philippines", "Indonesia"],
                    "language_hints": ["en"],
                },
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico", "Brazil"],
                    "country_validation_terms": ["Mexico", "Brazil"],
                    "language_hints": ["en", "es", "pt"],
                },
            },
            "competitors_by_region": {
                "sea": ["Grab"],
                "latam": ["Uber"],
            },
            "topic_groups": {
                "campaign_launches": ["campaign", "launch"],
            },
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "ignored_geo_terms": ["USA", "Europe"],
            "daily_digest_limit": 10,
            "enabled_providers": ["google_news_rss"],
        }
    )
    patch_runtime(monkeypatch, tmp_path, config)
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [
            StaticProvider(
                "mock_news",
                [
                    RawArticle(
                        title="Uber launches global driver campaign",
                        url="https://example.com/uber-global-driver-campaign",
                        provider="mock_provider",
                        source="Example News",
                        published_at="2026-05-19T09:00:00Z",
                        snippet="Uber launches a new driver campaign across multiple markets.",
                        query='"Uber" campaign launches Latin America',
                        region=None,
                        language="en",
                        competitor_hints=("Uber",),
                    )
                ],
            )
        ],
    )

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea", "latam"])

    assert len(result["analysis"].candidates) == 1
    assert result["analysis"].candidates[0].region == "latam"
    assert len(result["digest"].alerts) == 1
    assert result["alert_schemas"][0]["competitor"] == "Uber"
    assert result["alert_schemas"][0]["region"] == "LATAM"


def test_run_pipeline_deduplicates_shared_bolt_article_across_regions(tmp_path, monkeypatch):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "sea": {
                    "label": "Southeast Asia",
                    "geo_terms": ["Indonesia", "Thailand"],
                    "country_validation_terms": ["Indonesia", "Thailand"],
                    "language_hints": ["en"],
                },
                "cis": {
                    "label": "CIS / Central Asia",
                    "geo_terms": ["Kazakhstan", "Uzbekistan"],
                    "country_validation_terms": ["Kazakhstan", "Uzbekistan", "Almaty"],
                    "language_hints": ["en", "ru"],
                },
            },
            "competitors_by_region": {
                "sea": ["Bolt"],
                "cis": ["Bolt"],
            },
            "topic_groups": {
                "campaign_launches": ["campaign", "launch", "driver"],
            },
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "ignored_geo_terms": ["USA", "Europe"],
            "daily_digest_limit": 10,
            "enabled_providers": ["google_news_rss"],
        }
    )
    patch_runtime(monkeypatch, tmp_path, config)

    raw_articles = [
        RawArticle(
            title="Bolt launches driver campaign in Almaty",
            url="https://example.com/bolt-almaty-driver-campaign",
            provider="mock_provider",
            source="Example News",
            published_at="2026-05-19T09:00:00Z",
            snippet="Bolt launches a driver campaign in Almaty, Kazakhstan.",
            query='"Bolt" campaign launches Southeast Asia',
            region=None,
            language="en",
            competitor_hints=("Bolt",),
            metadata={
                "query_owner_competitor": "Bolt",
                "query_owner_region": "sea",
            },
        ),
        RawArticle(
            title="Bolt launches driver campaign in Almaty",
            url="https://mirror.example.com/bolt-almaty-driver-campaign",
            provider="mock_provider",
            source="Mirror News",
            published_at="2026-05-19T09:00:00Z",
            snippet="Bolt launches a driver campaign in Almaty, Kazakhstan.",
            query='"Bolt" campaign launches CIS / Central Asia',
            region=None,
            language="en",
            competitor_hints=("Bolt",),
            metadata={
                "query_owner_competitor": "Bolt",
                "query_owner_region": "cis",
            },
        ),
    ]

    def fake_collect_raw_articles(**kwargs):
        return raw_articles, ("mock_news",), {}

    def fake_prefilter(self, raw_articles, regions=None):
        return AnalysisResult(
            candidates=[
                CandidateArticle(
                    raw_article=raw_articles[0],
                    competitor="Bolt",
                    topic_group="campaign_launches",
                    score=8,
                    matched_keywords=("campaign", "launch", "driver"),
                    region=None,
                    country_hint=None,
                    language_hint="en",
                    reasons=("competitor_match",),
                    summary="Bolt launches driver campaign in Almaty",
                ),
                CandidateArticle(
                    raw_article=raw_articles[1],
                    competitor="Bolt",
                    topic_group="campaign_launches",
                    score=7,
                    matched_keywords=("campaign", "launch", "driver"),
                    region=None,
                    country_hint=None,
                    language_hint="en",
                    reasons=("competitor_match",),
                    summary="Bolt launches driver campaign in Almaty",
                ),
            ],
            dropped_count=0,
            dropped_articles=[],
        )

    monkeypatch.setattr(cli, "collect_raw_articles", fake_collect_raw_articles)
    monkeypatch.setattr(cli.CompetitorAnalyzer, "prefilter_raw_articles", fake_prefilter)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea", "cis"])

    assert len(result["analysis"].candidates) == 2
    assert len(result["digest"].alerts) == 1
    assert len(result["alert_schemas"]) == 1
    assert result["alert_schemas"][0]["competitor"] == "Bolt"
    assert result["alert_schemas"][0]["region"] == "cis"


def test_telegram_delivery_uses_dedicated_telegram_top_n_slice(tmp_path, monkeypatch):
    articles = build_capped_articles(16)
    config = build_config(
        daily_digest_limit=16,
        competitors_by_region={"sea": [item.competitor_hints[0] for item in articles]},
    )
    patch_runtime(
        monkeypatch,
        tmp_path,
        config,
        use_llm_alerts=True,
        llm_top_n=1,
        telegram_top_n=3,
    )
    monkeypatch.setattr(cli, "build_providers", lambda names: [StaticProvider("mock_news", articles)])

    class FakeAlertAnalyzer:
        def __init__(self, use_llm, model=None):
            self.use_llm = use_llm

        def analyze_candidate(self, candidate, *, article_context=None):
            return {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group,
                "priority": "MEDIUM",
                "what_happened": candidate.title,
                "why_it_matters": "llm" if self.use_llm else "fallback",
                "potential_impact": "impact",
                "recommended_action": "act",
                "confidence": 0.7,
            }

    sender_calls = {}

    class FakeTelegramSender:
        def __init__(self, storage, dry_run):
            sender_calls["dry_run"] = dry_run

        def send_daily_digest(self, alert_schemas, alerts, source_urls, generated_at):
            sender_calls["alerts"] = len(alerts)
            sender_calls["schemas"] = len(alert_schemas)
            sender_calls["fallback_count"] = sum(
                1 for item in alert_schemas if item["why_it_matters"] == "fallback"
            )
            return {"ok": True, "dry_run": True}

    monkeypatch.setattr(cli, "CompetitorAlertAnalyzer", FakeAlertAnalyzer)
    monkeypatch.setattr(cli, "TelegramSender", FakeTelegramSender)

    result = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="dry",
    )

    assert len(result["digest"].alerts) == 16
    assert len(result["alert_schemas"]) == 16
    assert result["alert_schemas"][-1]["why_it_matters"] == "fallback"
    assert sender_calls == {
        "dry_run": True,
        "alerts": 3,
        "schemas": 3,
        "fallback_count": 2,
    }


def test_unsent_telegram_alert_is_carried_over_on_next_run(tmp_path, monkeypatch):
    articles = build_capped_articles(16)
    config = build_config(
        daily_digest_limit=16,
        competitors_by_region={"sea": [item.competitor_hints[0] for item in articles]},
    )
    patch_runtime(monkeypatch, tmp_path, config)

    provider_calls = {"count": 0}

    def fake_build_providers(names):
        provider_calls["count"] += 1
        if provider_calls["count"] == 1:
            return [StaticProvider("mock_news", articles)]
        return [
            StaticProvider(
                "mock_news",
                [
                    article(
                        competitor="Comp0",
                        title="Comp0 launches new airport pricing program in the United States",
                        url="https://example.com/comp0-united-states-pricing",
                        query='"Comp0" pricing Southeast Asia',
                        snippet="Comp0 is piloting discount airport rides across the USA market.",
                    )
                ],
            )
        ]

    class FakeAlertAnalyzer:
        def __init__(self, use_llm, model=None):
            self.use_llm = use_llm

        def analyze_candidate(self, candidate, *, article_context=None):
            return {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group,
                "priority": "MEDIUM",
                "what_happened": candidate.title,
                "why_it_matters": "llm" if self.use_llm else "fallback",
                "potential_impact": "impact",
                "recommended_action": "act",
                "confidence": 0.7,
            }

    class FakeTelegramSender:
        def __init__(self, storage, dry_run):
            self.storage = storage
            self.chat_id = "12345"

        def send_daily_digest(self, alert_schemas, alerts, source_urls, generated_at):
            for alert in alerts:
                self.storage.mark_delivered(
                    alert_key=alert.digest_key,
                    channel="telegram",
                    delivered_at="2026-05-20T09:00:00Z",
                    destination=self.chat_id,
                )
            return {"ok": True, "dry_run": False, "message_id": "1"}

    monkeypatch.setattr(cli, "build_providers", fake_build_providers)
    monkeypatch.setattr(cli, "CompetitorAlertAnalyzer", FakeAlertAnalyzer)
    monkeypatch.setattr(cli, "TelegramSender", FakeTelegramSender)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    first = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="send",
    )
    second = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="send",
    )

    assert len(first["digest"].alerts) == 16
    assert len(second["digest"].alerts) == 1
    assert second["digest"].alerts[0].digest_key == (
        "comp15::market_entry::https://example.com/comp15-market-entry-15"
    )

    storage = SQLiteTrackerStorage(tmp_path / "output" / "tracker.db")
    deferred_candidates = storage.get_deferred_candidates(
        channel="telegram",
        destination="12345",
        max_age_days=2,
    )
    assert deferred_candidates == []


def test_no_fresh_ingest_does_not_replay_deferred_backlog_into_fresh_digest(
    tmp_path, monkeypatch
):
    articles = build_capped_articles(16)
    config = build_config(
        daily_digest_limit=16,
        competitors_by_region={"sea": [item.competitor_hints[0] for item in articles]},
    )
    patch_runtime(monkeypatch, tmp_path, config)

    provider_calls = {"count": 0}

    def fake_build_providers(names):
        provider_calls["count"] += 1
        if provider_calls["count"] == 1:
            return [StaticProvider("mock_news", articles)]
        return [StaticProvider("mock_news", [])]

    class FakeAlertAnalyzer:
        def __init__(self, use_llm, model=None):
            self.use_llm = use_llm

        def analyze_candidate(self, candidate, *, article_context=None):
            return {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group,
                "priority": "MEDIUM",
                "what_happened": candidate.title,
                "why_it_matters": "llm" if self.use_llm else "fallback",
                "potential_impact": "impact",
                "recommended_action": "act",
                "confidence": 0.7,
            }

    class FakeTelegramSender:
        def __init__(self, storage, dry_run):
            self.storage = storage
            self.chat_id = "12345"

        def send_daily_digest(self, alert_schemas, alerts, source_urls, generated_at):
            for alert in alerts:
                self.storage.mark_delivered(
                    alert_key=alert.digest_key,
                    channel="telegram",
                    delivered_at="2026-05-20T09:00:00Z",
                    destination=self.chat_id,
                )
            return {"ok": True, "dry_run": False, "message_id": "1"}

    monkeypatch.setattr(cli, "build_providers", fake_build_providers)
    monkeypatch.setattr(cli, "CompetitorAlertAnalyzer", FakeAlertAnalyzer)
    monkeypatch.setattr(cli, "TelegramSender", FakeTelegramSender)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    first = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="send",
    )
    second = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="send",
    )

    assert len(first["digest"].alerts) == 16
    assert second["digest"].alerts == ()
    assert second["telegram_result"] == {
        "ok": True,
        "skipped": True,
        "reason": "no_fresh_ingest",
        "message_id": None,
    }

    summary_payload = json.loads(second["summary_path"].read_text(encoding="utf-8"))
    assert summary_payload["raw_articles_fetched"] == 0
    assert summary_payload["alerts_created"] == 0
    assert summary_payload["status"] == "success_no_fresh_ingest"
    assert (
        summary_payload["provider_diagnostics"]["pipeline"]["warning"]
        == "No fresh ingest available for this run; deferred backlog was not used to build a fresh daily digest."
    )

    storage = SQLiteTrackerStorage(tmp_path / "output" / "tracker.db")
    deferred_candidates = storage.get_deferred_candidates(
        channel="telegram",
        destination="12345",
        max_age_days=2,
    )
    assert len(deferred_candidates) == 1
    assert deferred_candidates[0].raw_article.metadata["deferred_digest_key"] == first["digest"].alerts[-1].digest_key


def test_failed_telegram_send_does_not_create_deferred_history(tmp_path, monkeypatch):
    articles = build_capped_articles(16)
    config = build_config(
        daily_digest_limit=16,
        competitors_by_region={"sea": [item.competitor_hints[0] for item in articles]},
    )
    patch_runtime(monkeypatch, tmp_path, config)

    def fake_build_providers(names):
        return [StaticProvider("mock_news", articles)]

    class FakeAlertAnalyzer:
        def __init__(self, use_llm, model=None):
            self.use_llm = use_llm

        def analyze_candidate(self, candidate, *, article_context=None):
            return {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group,
                "priority": "MEDIUM",
                "what_happened": candidate.title,
                "why_it_matters": "llm" if self.use_llm else "fallback",
                "potential_impact": "impact",
                "recommended_action": "act",
                "confidence": 0.7,
            }

    class FailingTelegramSender:
        def __init__(self, storage, dry_run):
            self.storage = storage

        def send_daily_digest(self, alert_schemas, alerts, source_urls, generated_at):
            raise RuntimeError("telegram send failed")

    monkeypatch.setattr(cli, "build_providers", fake_build_providers)
    monkeypatch.setattr(cli, "CompetitorAlertAnalyzer", FakeAlertAnalyzer)
    monkeypatch.setattr(cli, "TelegramSender", FailingTelegramSender)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    with pytest.raises(RuntimeError, match="telegram send failed"):
        cli.run_pipeline(days=7, min_score=5, regions=["sea"], telegram_mode="send")

    storage = SQLiteTrackerStorage(tmp_path / "output" / "tracker.db")
    deferred_candidates = storage.get_deferred_candidates(
        channel="telegram",
        destination="12345",
        max_age_days=2,
    )
    assert deferred_candidates == []


def test_wrong_destination_delivery_does_not_block_next_configured_chat_run(
    tmp_path, monkeypatch
):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)
    shared_articles = [
        article(
            competitor="Grab",
            title="Grab launches driver support program in Manila",
            url="https://example.com/grab-driver-support-manila",
        )
    ]

    def fake_build_providers(names):
        return [StaticProvider("mock_news", shared_articles)]

    class FakeTelegramSender:
        def __init__(self, storage, dry_run):
            self.storage = storage

        def send_daily_digest(self, alert_schemas, alerts, source_urls, generated_at):
            for alert in alerts:
                self.storage.mark_delivered(
                    alert_key=alert.digest_key,
                    channel="telegram",
                    delivered_at="2026-05-20T09:00:00Z",
                    destination="wrong-chat",
                )
            return {"ok": True, "dry_run": False, "message_id": "1"}

    monkeypatch.setattr(cli, "build_providers", fake_build_providers)
    monkeypatch.setattr(cli, "TelegramSender", FakeTelegramSender)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    first = cli.run_pipeline(days=7, min_score=5, regions=["sea"], telegram_mode="send")
    second = cli.run_pipeline(days=7, min_score=5, regions=["sea"], telegram_mode="send")

    assert len(first["digest"].alerts) == 1
    assert len(second["digest"].alerts) == 1


def test_successful_telegram_delivery_suppresses_repeat_for_same_destination(
    tmp_path, monkeypatch
):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)
    shared_articles = [
        article(
            competitor="Grab",
            title="Grab launches driver support program in Manila",
            url="https://example.com/grab-driver-support-manila",
        )
    ]

    def fake_build_providers(names):
        return [StaticProvider("mock_news", shared_articles)]

    class FakeTelegramSender:
        def __init__(self, storage, dry_run):
            self.storage = storage

        def send_daily_digest(self, alert_schemas, alerts, source_urls, generated_at):
            for alert in alerts:
                self.storage.mark_delivered(
                    alert_key=alert.digest_key,
                    channel="telegram",
                    delivered_at="2026-05-20T09:00:00Z",
                    destination="12345",
                )
            return {"ok": True, "dry_run": False, "message_id": "1"}

    monkeypatch.setattr(cli, "build_providers", fake_build_providers)
    monkeypatch.setattr(cli, "TelegramSender", FakeTelegramSender)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    first = cli.run_pipeline(days=7, min_score=5, regions=["sea"], telegram_mode="send")
    second = cli.run_pipeline(days=7, min_score=5, regions=["sea"], telegram_mode="send")

    assert len(first["digest"].alerts) == 1
    assert len(second["digest"].alerts) == 0


def test_daily_marketing_digest_excludes_generic_industry_context_noise(tmp_path, monkeypatch):
    config = build_config(
        extra_topics={
            "industry_context": [
                "ride-hailing",
                "e-hailing",
                "on-demand mobility",
                "ride-sharing",
            ]
        }
    )
    patch_runtime(monkeypatch, tmp_path, config)

    raw_articles = [
        article(
            competitor="Grab",
            title="Shared mobility market to reach record growth by 2032",
            url="https://example.com/shared-mobility-forecast",
            query='"Grab" industry_context Southeast Asia',
            snippet="A broad ride-hailing market forecast mentions several platforms in passing.",
        )
    ]

    def fake_collect_raw_articles(**kwargs):
        return raw_articles, ("mock_news",), {}, len(raw_articles), {}

    def fake_prefilter(self, raw_articles, regions=None):
        return AnalysisResult(
            candidates=[
                CandidateArticle(
                    raw_article=raw_articles[0],
                    competitor="Grab",
                    topic_group="industry_context",
                    score=8,
                    matched_keywords=("ride-hailing",),
                    summary="Generic shared mobility market forecast.",
                    region="sea",
                    country_hint=None,
                    language_hint="en",
                    reasons=("competitor_mentioned", "topic_match:industry_context"),
                )
            ],
            dropped_count=0,
            dropped_articles=[],
        )

    monkeypatch.setattr(cli, "collect_raw_articles", fake_collect_raw_articles)
    monkeypatch.setattr(cli.CompetitorAnalyzer, "prefilter_raw_articles", fake_prefilter)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert result["digest"].alerts == ()
    assert len(result["analysis"].candidates) == 1


def test_daily_marketing_digest_keeps_actionable_industry_context(tmp_path, monkeypatch):
    config = build_config(
        extra_topics={
            "industry_context": [
                "ride-hailing",
                "e-hailing",
                "on-demand mobility",
                "ride-sharing",
            ]
        }
    )
    patch_runtime(monkeypatch, tmp_path, config)

    raw_articles = [
        article(
            competitor="Grab",
            title="Grab launches new driver recruitment campaign in Manila",
            url="https://example.com/grab-driver-recruitment",
            query='"Grab" industry_context Southeast Asia',
            snippet="Grab expands its ride-hailing operations with a new driver recruitment push.",
        )
    ]

    def fake_collect_raw_articles(**kwargs):
        return raw_articles, ("mock_news",), {}, len(raw_articles), {}

    def fake_prefilter(self, raw_articles, regions=None):
        return AnalysisResult(
            candidates=[
                CandidateArticle(
                    raw_article=raw_articles[0],
                    competitor="Grab",
                    topic_group="industry_context",
                    score=8,
                    matched_keywords=("ride-hailing", "driver recruitment campaign"),
                    summary="Grab launches new driver recruitment campaign in Manila.",
                    region="sea",
                    country_hint="Philippines",
                    language_hint="en",
                    reasons=("competitor_mentioned", "priority_signal"),
                )
            ],
            dropped_count=0,
            dropped_articles=[],
        )

    monkeypatch.setattr(cli, "collect_raw_articles", fake_collect_raw_articles)
    monkeypatch.setattr(cli.CompetitorAnalyzer, "prefilter_raw_articles", fake_prefilter)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert len(result["digest"].alerts) == 1
    assert result["digest"].alerts[0].topic_group == "industry_context"


def test_daily_marketing_digest_excludes_non_marketing_topic_groups_by_default(tmp_path, monkeypatch):
    config = build_config(
        extra_topics={
            "industry_context": ["ride-hailing"],
            "product_features_innovation": ["safety features", "fixed price"],
        }
    )
    patch_runtime(monkeypatch, tmp_path, config)

    raw_articles = [
        article(
            competitor="Grab",
            title="Grab broad industry mention in policy round-up",
            url="https://example.com/grab-policy-roundup",
            query='"Grab" industry_context Southeast Asia',
        ),
        article(
            competitor="Grab",
            title="Grab pilots new fixed price airport rides in Manila",
            url="https://example.com/grab-fixed-price-manila",
            query='"Grab" product_features_innovation Southeast Asia',
        ),
    ]

    def fake_collect_raw_articles(**kwargs):
        return raw_articles, ("mock_news",), {}, len(raw_articles), {}

    def fake_prefilter(self, raw_articles, regions=None):
        return AnalysisResult(
            candidates=[
                CandidateArticle(
                    raw_article=raw_articles[0],
                    competitor="Grab",
                    topic_group="industry_context",
                    score=7,
                    matched_keywords=("ride-hailing",),
                    summary="Broad policy round-up.",
                    region="sea",
                    country_hint=None,
                    language_hint="en",
                    reasons=("competitor_mentioned",),
                ),
                CandidateArticle(
                    raw_article=raw_articles[1],
                    competitor="Grab",
                    topic_group="product_features_innovation",
                    score=8,
                    matched_keywords=("fixed price",),
                    summary="Grab pilots new fixed price airport rides in Manila.",
                    region="sea",
                    country_hint="Philippines",
                    language_hint="en",
                    reasons=("competitor_mentioned", "priority_signal"),
                ),
            ],
            dropped_count=0,
            dropped_articles=[],
        )

    monkeypatch.setattr(cli, "collect_raw_articles", fake_collect_raw_articles)
    monkeypatch.setattr(cli.CompetitorAnalyzer, "prefilter_raw_articles", fake_prefilter)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert len(result["digest"].alerts) == 1
    assert result["digest"].alerts[0].topic_group == "product_features_innovation"


def test_daily_marketing_digest_rejects_off_region_article_for_latam(tmp_path, monkeypatch):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico", "Brazil", "Colombia", "LATAM"],
                    "country_validation_terms": ["Mexico", "Brazil", "Colombia", "Argentina"],
                    "language_hints": ["en", "es", "pt"],
                }
            },
            "competitors_by_region": {"latam": ["Uber"]},
            "topic_groups": {"campaign_launches": ["campaign", "partnership"]},
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "daily_digest_limit": 10,
            "enabled_providers": ["google_news_rss"],
        }
    )
    patch_runtime(monkeypatch, tmp_path, config)

    raw_articles = [
        article(
            competitor="Uber",
            title="Uber launches Pune Metro partnership in India",
            url="https://example.com/uber-pune-metro-india",
            query='"Uber" campaign_launches Latin America',
            source="Transit India",
            region="latam",
        )
    ]

    def fake_collect_raw_articles(**kwargs):
        return raw_articles, ("mock_news",), {}, len(raw_articles), {}

    def fake_prefilter(self, raw_articles, regions=None):
        return AnalysisResult(
            candidates=[
                CandidateArticle(
                    raw_article=raw_articles[0],
                    competitor="Uber",
                    topic_group="campaign_launches",
                    score=8,
                    matched_keywords=("partnership",),
                    summary="Uber launches Pune Metro partnership in India.",
                    region="latam",
                    country_hint=None,
                    language_hint="en",
                    reasons=("competitor_mentioned", "priority_signal"),
                )
            ],
            dropped_count=0,
            dropped_articles=[],
        )

    monkeypatch.setattr(cli, "collect_raw_articles", fake_collect_raw_articles)
    monkeypatch.setattr(cli.CompetitorAnalyzer, "prefilter_raw_articles", fake_prefilter)

    result = cli.run_pipeline(days=7, min_score=5, regions=["latam"])

    assert result["digest"].alerts == ()
    assert len(result["analysis"].candidates) == 1


def test_daily_marketing_digest_keeps_valid_in_region_article_for_latam(tmp_path, monkeypatch):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico", "Brazil", "Colombia", "LATAM"],
                    "country_validation_terms": ["Mexico", "Brazil", "Colombia", "Argentina"],
                    "language_hints": ["en", "es", "pt"],
                }
            },
            "competitors_by_region": {"latam": ["Uber"]},
            "topic_groups": {"campaign_launches": ["campaign", "partnership"]},
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "daily_digest_limit": 10,
            "enabled_providers": ["google_news_rss"],
        }
    )
    patch_runtime(monkeypatch, tmp_path, config)

    raw_articles = [
        article(
            competitor="Uber",
            title="Uber launches airport partnership in Mexico City",
            url="https://example.com/uber-mexico-airport",
            query='"Uber" campaign_launches Latin America',
            source="El Financiero",
            region="latam",
        )
    ]

    def fake_collect_raw_articles(**kwargs):
        return raw_articles, ("mock_news",), {}, len(raw_articles), {}

    def fake_prefilter(self, raw_articles, regions=None):
        return AnalysisResult(
            candidates=[
                CandidateArticle(
                    raw_article=raw_articles[0],
                    competitor="Uber",
                    topic_group="campaign_launches",
                    score=8,
                    matched_keywords=("partnership",),
                    summary="Uber launches airport partnership in Mexico City.",
                    region="latam",
                    country_hint="Mexico",
                    language_hint="en",
                    reasons=("competitor_mentioned", "priority_signal"),
                )
            ],
            dropped_count=0,
            dropped_articles=[],
        )

    monkeypatch.setattr(cli, "collect_raw_articles", fake_collect_raw_articles)
    monkeypatch.setattr(cli.CompetitorAnalyzer, "prefilter_raw_articles", fake_prefilter)

    result = cli.run_pipeline(days=7, min_score=5, regions=["latam"])

    assert len(result["digest"].alerts) == 1
    assert result["digest"].alerts[0].candidate.country_hint == "Mexico"


def test_daily_marketing_digest_handles_ambiguous_geography_conservatively(tmp_path, monkeypatch):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico", "Brazil", "Colombia", "LATAM"],
                    "country_validation_terms": ["Mexico", "Brazil", "Colombia", "Argentina"],
                    "language_hints": ["en", "es", "pt"],
                }
            },
            "competitors_by_region": {"latam": ["Uber"]},
            "topic_groups": {"campaign_launches": ["campaign", "partnership"]},
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "daily_digest_limit": 10,
            "enabled_providers": ["google_news_rss"],
        }
    )
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Uber launches airport partnership for commuters",
            url="https://example.com/uber-ambiguous-airport",
            provider="mock_provider",
            source="Mobility Brief",
            snippet="Uber launches airport partnership for commuters",
            query='"Uber" campaign_launches Latin America',
            region=None,
        ),
        competitor="Uber",
        topic_group="campaign_launches",
        score=8,
        matched_keywords=("partnership",),
        summary="Uber launches airport partnership for commuters.",
        region=None,
        country_hint=None,
        language_hint="en",
        reasons=("competitor_mentioned", "priority_signal"),
    )

    digest = cli.DigestBuilder().build(
        competitors=["Uber"],
        candidates=[candidate],
        regions=["latam"],
        apply_marketing_filters=True,
        marketing_config=config,
    )

    assert digest.alerts == ()


def test_daily_marketing_digest_rejects_noise_domains_and_forecast_sources(tmp_path, monkeypatch):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)

    raw_articles = [
        article(
            competitor="Grab",
            title="DiDi Ride Hailing Market Forecast 2032",
            url="https://www.openpr.com/news/123456/didi-forecast-2032",
            query='"Grab" campaign_launches Southeast Asia',
            source="openPR",
        ),
        article(
            competitor="Grab",
            title="Uber expands points and miles travel rewards tie-up",
            url="https://thepointsguy.com/news/uber-points-miles-tie-up",
            query='"Grab" campaign_launches Southeast Asia',
            source="The Points Guy",
        ),
        article(
            competitor="Grab",
            title="Grab launches airport partnership in Manila",
            url="https://example.com/grab-manila-airport",
            query='"Grab" campaign_launches Southeast Asia',
            source="CNA Asia",
        ),
    ]

    def fake_collect_raw_articles(**kwargs):
        return raw_articles, ("mock_news",), {}, len(raw_articles), {}

    def fake_prefilter(self, raw_articles, regions=None):
        return AnalysisResult(
            candidates=[
                CandidateArticle(
                    raw_article=raw_articles[0],
                    competitor="DiDi",
                    topic_group="campaign_launches",
                    score=7,
                    matched_keywords=("campaign",),
                    summary="Forecast page.",
                    region="sea",
                    country_hint="Philippines",
                    language_hint="en",
                    reasons=("competitor_mentioned",),
                ),
                CandidateArticle(
                    raw_article=raw_articles[1],
                    competitor="Uber",
                    topic_group="campaign_launches",
                    score=7,
                    matched_keywords=("partnership",),
                    summary="Travel rewards lifestyle page.",
                    region="sea",
                    country_hint="Philippines",
                    language_hint="en",
                    reasons=("competitor_mentioned",),
                ),
                CandidateArticle(
                    raw_article=raw_articles[2],
                    competitor="Grab",
                    topic_group="campaign_launches",
                    score=8,
                    matched_keywords=("partnership",),
                    summary="Grab launches airport partnership in Manila.",
                    region="sea",
                    country_hint="Philippines",
                    language_hint="en",
                    reasons=("competitor_mentioned", "priority_signal"),
                ),
            ],
            dropped_count=0,
            dropped_articles=[],
        )

    monkeypatch.setattr(cli, "collect_raw_articles", fake_collect_raw_articles)
    monkeypatch.setattr(cli.CompetitorAnalyzer, "prefilter_raw_articles", fake_prefilter)

    result = cli.run_pipeline(days=7, min_score=5, regions=["sea"])

    assert len(result["digest"].alerts) == 1
    assert result["digest"].alerts[0].candidate.url == "https://example.com/grab-manila-airport"


def test_daily_marketing_digest_rejects_noisy_google_news_secondary_item(tmp_path, monkeypatch):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)

    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Uber mobility trends update for urban commuters",
            url="https://news.google.com/rss/articles/secondary-noise",
            provider="google_news_rss",
            source="Google News",
            snippet="A broad mobility commentary mentions Uber in passing.",
            query='"Uber" campaign_launches Southeast Asia',
            region="sea",
            metadata={"source_tier": "tier1_aggregator"},
        ),
        competitor="Uber",
        topic_group="campaign_launches",
        score=7,
        matched_keywords=("campaign",),
        summary="Broad commentary mention.",
        region="sea",
        country_hint=None,
        language_hint="en",
        reasons=("competitor_mentioned",),
    )

    digest = cli.DigestBuilder().build(
        competitors=["Uber"],
        candidates=[candidate],
        regions=["sea"],
        apply_marketing_filters=True,
        marketing_config=config,
    )

    assert digest.alerts == ()


def test_daily_marketing_digest_keeps_actionable_google_news_secondary_item(tmp_path, monkeypatch):
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)

    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Grab launches airport partnership in Manila",
            url="https://news.google.com/rss/articles/grab-manila-airport",
            provider="google_news_rss",
            source="Google News",
            snippet="Grab launches a new airport partnership in Manila with driver incentives.",
            query='"Grab" campaign_launches Southeast Asia',
            region="sea",
            metadata={"source_tier": "tier1_aggregator"},
        ),
        competitor="Grab",
        topic_group="campaign_launches",
        score=8,
        matched_keywords=("campaign", "partnership"),
        summary="Grab launches airport partnership in Manila.",
        region="sea",
        country_hint="Philippines",
        language_hint="en",
        reasons=("competitor_mentioned", "priority_signal"),
    )

    digest = cli.DigestBuilder().build(
        competitors=["Grab"],
        candidates=[candidate],
        regions=["sea"],
        apply_marketing_filters=True,
        marketing_config=config,
    )

    assert len(digest.alerts) == 1
    assert digest.alerts[0].candidate.provider == "google_news_rss"


def test_articles_older_than_seven_days_are_archived_but_filtered_from_digest_and_telegram(
    tmp_path, monkeypatch
):
    freeze_cli_today(monkeypatch)
    config = build_config()
    runtime = patch_runtime(monkeypatch, tmp_path, config)
    old_article = article(
        competitor="Grab",
        title="Grab old campaign in Manila",
        url="https://example.com/grab-old-campaign",
        published_at="2026-05-13T09:00:00Z",
    )
    fresh_article = article(
        competitor="Gojek",
        title="Gojek launches women driver partnership program in Cebu",
        url="https://example.com/gojek-fresh-campaign",
        published_at="2026-05-20T09:00:00Z",
    )
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [StaticProvider("mock_news", [old_article, fresh_article])],
    )

    sender_calls = {}

    class FakeTelegramSender:
        def __init__(self, storage, dry_run):
            sender_calls["dry_run"] = dry_run

        def send_daily_digest(self, alert_schemas, alerts, source_urls, generated_at):
            sender_calls["alerts"] = len(alerts)
            sender_calls["urls"] = list(source_urls)
            return {"ok": True, "dry_run": True}

    monkeypatch.setattr(cli, "TelegramSender", FakeTelegramSender)

    result = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="dry",
        export_csv=True,
    )

    assert len(result["digest"].alerts) == 1
    assert result["digest"].alerts[0].candidate.url == "https://example.com/gojek-fresh-campaign"
    assert result["expired_alerts_count"] == 1
    assert sender_calls == {
        "dry_run": True,
        "alerts": 1,
        "urls": ["https://example.com/gojek-fresh-campaign"],
    }

    csv_text = result["candidates_csv_path"].read_text(encoding="utf-8")
    assert "is_expired" in csv_text
    assert "https://example.com/grab-old-campaign" in csv_text
    assert "True" in csv_text

    with sqlite3.connect(runtime.database_path) as connection:
        row = connection.execute(
            "SELECT metadata_json FROM articles_raw WHERE url = ?",
            ("https://example.com/grab-old-campaign",),
        ).fetchone()
    metadata = json.loads(row[0])
    assert metadata["is_expired"] is True


def test_old_high_signal_article_gets_green_corridor_when_new_and_above_average(
    tmp_path, monkeypatch
):
    freeze_cli_today(monkeypatch)
    config = build_config(
        competitors_by_region={"sea": ["Grab", "Gojek", "Bolt"]},
    )
    patch_runtime(monkeypatch, tmp_path, config)
    old_high_signal = article(
        competitor="Grab",
        title="Grab launches major campaign in Manila",
        url="https://example.com/grab-major-campaign",
        published_at="2026-05-10T09:00:00Z",
    )
    fresh_low_signal_one = low_signal_article(
        competitor="Gojek",
        title="Gojek campaign update",
        url="https://example.com/gojek-campaign-update",
        published_at="2026-05-20T09:00:00Z",
    )
    fresh_low_signal_two = low_signal_article(
        competitor="Bolt",
        title="Bolt campaign teaser",
        url="https://example.com/bolt-campaign-teaser",
        published_at="2026-05-20T10:00:00Z",
    )
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [
            StaticProvider(
                "mock_news",
                [old_high_signal, fresh_low_signal_one, fresh_low_signal_two],
            )
        ],
    )

    result = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="dry",
        export_csv=True,
    )

    delivered_urls = [alert.candidate.url for alert in result["digest"].alerts]
    assert "https://example.com/grab-major-campaign" in delivered_urls
    assert result["expired_alerts_count"] == 0

    csv_text = result["candidates_csv_path"].read_text(encoding="utf-8")
    assert "https://example.com/grab-major-campaign" in csv_text
    assert "False" in csv_text


def test_undated_articles_are_expired_by_default_when_no_date_can_be_resolved(
    tmp_path, monkeypatch
):
    freeze_cli_today(monkeypatch)
    config = build_config()
    runtime = patch_runtime(monkeypatch, tmp_path, config)
    undated_article = article(
        competitor="Grab",
        title="Grab launches driver program in Manila",
        url="https://example.com/grab-undated-driver-program",
        published_at=None,
    )
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [StaticProvider("mock_news", [undated_article])],
    )

    result = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="dry",
        export_csv=True,
    )

    assert len(result["digest"].alerts) == 0
    assert result["expired_alerts_count"] == 1

    csv_text = result["candidates_csv_path"].read_text(encoding="utf-8")
    assert "https://example.com/grab-undated-driver-program" in csv_text
    assert "True" in csv_text

    with sqlite3.connect(runtime.database_path) as connection:
        row = connection.execute(
            "SELECT metadata_json FROM articles_raw WHERE url = ?",
            ("https://example.com/grab-undated-driver-program",),
        ).fetchone()
    metadata = json.loads(row[0])
    assert metadata["is_expired"] is True


def test_undated_articles_can_pass_only_with_explicit_allow_flag(
    tmp_path, monkeypatch
):
    freeze_cli_today(monkeypatch)
    config = build_config()
    runtime = patch_runtime(monkeypatch, tmp_path, config)
    undated_article = article(
        competitor="Grab",
        title="Grab launches driver program in Manila",
        url="https://example.com/grab-undated-driver-program-allowed",
        published_at=None,
        metadata={"allow_undated": True},
    )
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [StaticProvider("mock_news", [undated_article])],
    )

    result = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="dry",
        export_csv=True,
    )

    assert len(result["digest"].alerts) == 1
    assert result["expired_alerts_count"] == 0
    assert result["digest"].alerts[0].candidate.url == "https://example.com/grab-undated-driver-program-allowed"

    with sqlite3.connect(runtime.database_path) as connection:
        row = connection.execute(
            "SELECT metadata_json FROM articles_raw WHERE url = ?",
            ("https://example.com/grab-undated-driver-program-allowed",),
        ).fetchone()
    metadata = json.loads(row[0])
    assert metadata["is_expired"] is False


@pytest.mark.parametrize(
    ("resolved_source", "resolved_date"),
    [
        ("html_scraped", "2026-05-20"),
        ("llm", "2026-05-20"),
    ],
)
def test_freshness_gate_prefers_resolved_schema_date_over_stale_provider_date(
    tmp_path,
    monkeypatch,
    resolved_source,
    resolved_date,
):
    freeze_cli_today(monkeypatch)
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)
    stale_provider_article = article(
        competitor="Grab",
        title="Grab campaign with corrected final publication date",
        url=f"https://example.com/grab-resolved-{resolved_source}",
        published_at="2026-05-10T09:00:00Z",
    )
    monkeypatch.setattr(
        cli,
        "build_providers",
        lambda names: [StaticProvider("mock_news", [stale_provider_article])],
    )

    class FakeAlertAnalyzer:
        def __init__(self, use_llm, model=None, config=None):
            self.use_llm = use_llm

        def analyze_candidate(self, candidate, *, article_context=None):
            resolved = date.fromisoformat(resolved_date)
            return {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group,
                "priority": "MEDIUM",
                "published_date": resolved.isoformat(),
                "published_date_source": resolved_source,
                "resolved_publication_date": resolved,
                "resolved_publication_date_source": resolved_source,
                "what_happened": candidate.title,
                "why_it_matters": "resolved date should control freshness",
                "potential_impact": "impact",
                "recommended_action": "act",
                "confidence": 0.7,
            }

    monkeypatch.setattr(cli, "CompetitorAlertAnalyzer", FakeAlertAnalyzer)

    result = cli.run_pipeline(
        days=7,
        min_score=5,
        regions=["sea"],
        telegram_mode="dry",
    )

    assert len(result["digest"].alerts) == 1
    assert result["expired_alerts_count"] == 0
    assert result["alert_schemas"][0]["resolved_publication_date"].isoformat() == resolved_date
    assert result["alert_schemas"][0]["resolved_publication_date_source"] == resolved_source


def test_preranking_html_date_overrides_stale_provider_date_and_changes_digest_order(
    tmp_path, monkeypatch
):
    freeze_cli_today(monkeypatch)
    config = build_config()
    patch_runtime(monkeypatch, tmp_path, config)
    stale_provider_article = article(
        competitor="Grab",
        title="Grab launches campaign with stale provider date",
        url="https://example.com/grab-stale-provider-date",
        published_at="2026-05-10T09:00:00Z",
    )
    fresh_provider_article = article(
        competitor="Gojek",
        title="Gojek launches campaign with fresh provider date",
        url="https://example.com/gojek-fresh-provider-date",
        published_at="2026-05-19T09:00:00Z",
    )
    monkeypatch.setattr(
        cli,
        "collect_raw_articles",
        lambda **kwargs: (
            [fresh_provider_article, stale_provider_article],
            ["mock_news"],
            {},
        ),
    )

    extractor_calls = []

    class FakeContextExtractor:
        def __init__(self, *args, **kwargs):
            pass

        def extract(self, candidate):
            extractor_calls.append(candidate.url)
            published_at = (
                "2026-05-20"
                if candidate.url == "https://example.com/grab-stale-provider-date"
                else None
            )
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body=f"body for {candidate.title}",
                published_at=published_at,
                published_at_source="html_scraped" if published_at else None,
            )

        def build_fallback_context(self, candidate):
            return ArticleContext(
                title=candidate.title,
                snippet=candidate.raw_article.snippet,
                source_url=candidate.url,
                article_body="",
                published_at=None,
                published_at_source=None,
            )

    monkeypatch.setattr(cli, "ArticleContextExtractor", FakeContextExtractor)

    result = cli.run_pipeline(
        days=30,
        min_score=5,
        regions=["sea"],
    )

    assert extractor_calls == ["https://example.com/grab-stale-provider-date"]
    assert [alert.candidate.url for alert in result["digest"].alerts[:2]] == [
        "https://example.com/grab-stale-provider-date",
        "https://example.com/gojek-fresh-provider-date",
    ]
    assert result["alert_schemas"][0]["resolved_publication_date"].isoformat() == "2026-05-20"
    assert result["alert_schemas"][0]["resolved_publication_date_source"] == "html_scraped"
