import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from competitor_tracker import cli
from competitor_tracker.analyzer import AnalysisResult
from competitor_tracker.config import TrackerConfig, TrackerRuntimeConfig
from competitor_tracker.models import ArticleContext, CandidateArticle, RawArticle
from competitor_tracker.providers import ProviderError
from competitor_tracker.storage import SQLiteTrackerStorage


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
    assert result["alert_schemas"][0]["region"] == "latam"


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
