import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from competitor_tracker import cli
from competitor_tracker.config import TrackerConfig, TrackerRuntimeConfig
from competitor_tracker.models import ArticleContext, RawArticle
from competitor_tracker.providers import ProviderError
from competitor_tracker.storage import SQLiteTrackerStorage


def freeze_cli_today(monkeypatch, *, iso_date: str = "2026-05-21") -> None:
    monkeypatch.setattr(cli, "_today", lambda: date.fromisoformat(iso_date))


def build_config(
    *,
    daily_digest_limit: int = 10,
    extra_topics: dict | None = None,
    competitors_by_region: dict | None = None,
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


def patch_runtime(monkeypatch, tmp_path, config: TrackerConfig):
    runtime = build_runtime(tmp_path)
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


def test_duplicate_suppression_across_runs_uses_sqlite_history(tmp_path, monkeypatch):
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
    assert len(second["digest"].alerts) == 0


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
    patch_runtime(monkeypatch, tmp_path, config)
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
        def __init__(self, api_key=None):
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
    assert alert["region"] == candidate.region
    assert alert["country"] == expected_country
    assert alert["competitor_source"] == "pipeline"
    assert alert["region_source"] == "pipeline"
    assert alert["country_source"] == expected_country_source
    assert alert["geo_validation_fallback"] is expected_fallback


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
    patch_runtime(monkeypatch, tmp_path, config)
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


def test_telegram_delivery_uses_only_llm_targeted_top_slice(tmp_path, monkeypatch):
    articles = build_capped_articles(16)
    config = build_config(
        daily_digest_limit=16,
        competitors_by_region={"sea": [item.competitor_hints[0] for item in articles]},
    )
    patch_runtime(monkeypatch, tmp_path, config)
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
        "alerts": 15,
        "schemas": 15,
        "fallback_count": 0,
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
    assert len(second["digest"].alerts) == 1
    assert second["digest"].alerts[0].digest_key == first["digest"].alerts[-1].digest_key

    storage = SQLiteTrackerStorage(tmp_path / "output" / "tracker.db")
    deferred_candidates = storage.get_deferred_candidates(
        channel="telegram",
        destination="12345",
        max_age_days=2,
    )
    assert deferred_candidates == []


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
