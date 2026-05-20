import json
from pathlib import Path

from competitor_tracker import cli
from competitor_tracker.config import TrackerConfig, TrackerRuntimeConfig
from competitor_tracker.models import RawArticle
from competitor_tracker.providers import ProviderError
from competitor_tracker.storage import SQLiteTrackerStorage


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
