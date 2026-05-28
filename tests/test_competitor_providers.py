import sqlite3

import pytest

from competitor_tracker.cli import _extract_provider_metrics, collect_raw_articles
from competitor_tracker.config import TrackerConfig, TrackerRuntimeConfig
from competitor_tracker.models import CandidateArticle, RawArticle
from competitor_tracker.providers import (
    GdeltProvider,
    GuardianProvider,
    GoogleNewsRssProvider,
    NewsApiProvider,
    ProviderError,
    ProviderRequest,
    RegionalRssProvider,
    build_providers,
    supported_provider_names,
)
from competitor_tracker.storage import SQLiteTrackerStorage


class FakeResponse:
    def __init__(
        self,
        *,
        content: bytes = b"",
        payload: dict | None = None,
        status_code: int = 200,
    ) -> None:
        self.content = content
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


class FakeProvider:
    name = "fake_provider"

    def fetch(self, request: ProviderRequest):
        return [
            RawArticle(
                title="Uber launches airport rides in Mexico City",
                url="https://example.com/uber-airport?utm_source=test",
                provider=self.name,
                source="Example News",
                published_at="2026-05-18T09:00:00Z",
                snippet="Airport expansion.",
                query=request.queries[0],
                competitor_hints=request.competitor_hints_for_query(request.queries[0]),
            ),
            RawArticle(
                title="Uber launches airport rides in Mexico City | Another Publisher",
                url="https://example.com/uber-airport-copy",
                provider=self.name,
                source="Another Publisher",
                published_at="Tue, 18 May 2026 09:00:00 GMT",
                snippet="Airport expansion copy.",
                query=request.queries[0],
                competitor_hints=request.competitor_hints_for_query(request.queries[0]),
            ),
        ]


def test_google_news_rss_provider_fetches_raw_articles():
    xml_payload = b"""
    <rss>
      <channel>
        <item>
          <title>Uber launches airport rides in Mexico City</title>
          <link>https://example.com/uber-airport</link>
          <pubDate>Tue, 18 May 2026 09:00:00 GMT</pubDate>
          <description><![CDATA[<p>Expansion update</p>]]></description>
          <source>Example News</source>
        </item>
      </channel>
    </rss>
    """
    provider = GoogleNewsRssProvider(session=FakeSession([FakeResponse(content=xml_payload)]))

    articles, diagnostics = provider.fetch_with_diagnostics(
        ProviderRequest(
            competitors=("Uber",),
            days=7,
            queries=['"Uber" launch Mexico'],
        )
    )

    assert len(articles) == 1
    assert articles[0].provider == "google_news_rss"
    assert articles[0].source == "Example News"
    assert articles[0].query == '"Uber" launch Mexico'
    assert diagnostics["items_found"] == 1
    assert diagnostics["items_after_filter"] == 1
    assert diagnostics["queries"][0]["request_url"].startswith(
        "https://news.google.com/rss/search?q=%22Uber%22"
    )


def test_gdelt_provider_fetches_raw_articles():
    payload = {
        "articles": [
            {
                "title": "Bolt expands courier partnership in Kenya",
                "url": "https://example.com/bolt-kenya",
                "domain": "example.com",
                "seendate": "2026-05-18T09:00:00Z",
                "snippet": "Courier partnership update.",
            }
        ]
    }
    provider = GdeltProvider(session=FakeSession([FakeResponse(payload=payload)]))

    articles, diagnostics = provider.fetch_with_diagnostics(
        ProviderRequest(
            competitors=("Bolt",),
            days=7,
            queries=['"Bolt" partnership Kenya'],
        )
    )

    assert len(articles) == 1
    assert articles[0].provider == "gdelt"
    assert articles[0].source == "example.com"
    assert articles[0].query == '"Bolt" partnership Kenya'
    assert diagnostics["items_found"] == 1
    assert diagnostics["items_after_filter"] == 1
    assert "api.gdeltproject.org" in diagnostics["queries"][0]["request_url"]


def test_newsapi_provider_fetches_raw_articles(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "COMPETITOR_TRACKER_NEWSAPI_CACHE_PATH",
        str(tmp_path / "newsapi_cache.json"),
    )
    monkeypatch.setenv(
        "COMPETITOR_TRACKER_NEWSAPI_BUDGET_PATH",
        str(tmp_path / "newsapi_budget.json"),
    )
    payload = {
        "status": "ok",
        "articles": [
            {
                "title": "Uber launches airport rides in Mexico City",
                "url": "https://example.com/uber-airport-newsapi",
                "publishedAt": "2026-05-18T09:00:00Z",
                "description": "Airport expansion update.",
                "source": {"name": "Example News"},
            }
        ],
    }
    provider = NewsApiProvider(
        session=FakeSession([FakeResponse(payload=payload)]),
        api_key="test-key",
    )

    articles, diagnostics = provider.fetch_with_diagnostics(
        ProviderRequest(
            competitors=("Uber",),
            days=7,
            queries=['"Uber" launch Mexico'],
        )
    )

    assert len(articles) == 1
    assert articles[0].provider == "newsapi"
    assert articles[0].source == "Example News"
    assert articles[0].query == '"Uber" launch Mexico'
    assert diagnostics["queries"][0]["request_url"].endswith("apiKey=%2A%2A%2A")
    assert diagnostics["queries"][0]["http_status"] == 200


def test_newsapi_provider_surfaces_missing_api_key():
    provider = NewsApiProvider(
        session=FakeSession([]),
        api_key="",
    )

    with pytest.raises(ProviderError, match="NEWS_API_KEY is missing"):
        provider.fetch(
            ProviderRequest(
                competitors=("Uber",),
                days=7,
                queries=['"Uber" launch Mexico'],
            )
        )


def test_guardian_provider_fetches_raw_articles():
    payload = {
        "response": {
            "status": "ok",
            "results": [
                {
                    "webTitle": "Uber expands airport rides in Mexico City",
                    "webUrl": "https://www.theguardian.com/world/2026/may/18/uber-mexico-city",
                    "webPublicationDate": "2026-05-18T09:00:00Z",
                    "fields": {
                        "trailText": "Expansion update from Mexico City.",
                    },
                }
            ],
        }
    }
    provider = GuardianProvider(
        session=FakeSession([FakeResponse(payload=payload)]),
        api_key="guardian-test-key",
    )

    articles, diagnostics = provider.fetch_with_diagnostics(
        ProviderRequest(
            competitors=("Uber",),
            days=7,
            queries=['"Uber" launch Mexico'],
        )
    )

    assert len(articles) == 1
    assert articles[0].provider == "guardian"
    assert articles[0].source == "The Guardian"
    assert articles[0].metadata["source_tier"] == "tier2_direct"
    assert "api-key=%2A%2A%2A" in diagnostics["queries"][0]["request_url"]


def test_guardian_provider_skips_when_api_key_is_missing():
    provider = GuardianProvider(session=FakeSession([]), api_key="")

    articles, diagnostics = provider.fetch_with_diagnostics(
        ProviderRequest(
            competitors=("Uber",),
            days=7,
            queries=['"Uber" launch Mexico'],
        )
    )

    assert articles == []
    assert diagnostics["status"] == "skipped"


def test_guardian_provider_uses_local_cache_for_repeated_query(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "COMPETITOR_TRACKER_GUARDIAN_CACHE_PATH",
        str(tmp_path / "guardian_cache.json"),
    )
    monkeypatch.setenv(
        "COMPETITOR_TRACKER_GUARDIAN_BUDGET_PATH",
        str(tmp_path / "guardian_budget.json"),
    )
    monkeypatch.setenv("COMPETITOR_TRACKER_GUARDIAN_CACHE_TTL_SECONDS", "900")
    payload = {
        "response": {
            "status": "ok",
            "results": [
                {
                    "webTitle": "Uber expands airport rides in Mexico City",
                    "webUrl": "https://www.theguardian.com/world/2026/may/18/uber-mexico-city",
                    "webPublicationDate": "2026-05-18T09:00:00Z",
                    "fields": {"trailText": "Expansion update from Mexico City."},
                }
            ],
        }
    }
    session = FakeSession([FakeResponse(payload=payload)])
    provider = GuardianProvider(session=session, api_key="guardian-test-key")
    request = ProviderRequest(
        competitors=("Uber",),
        days=7,
        queries=['"Uber" launch Mexico'],
    )

    provider.fetch_with_diagnostics(request)
    articles, diagnostics = provider.fetch_with_diagnostics(request)

    assert len(session.calls) == 1
    assert len(articles) == 1
    assert diagnostics["queries"][0]["status"] == "cached"
    assert diagnostics["queries"][0]["cached"] is True


def test_guardian_provider_stops_when_daily_budget_is_exhausted(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "COMPETITOR_TRACKER_GUARDIAN_CACHE_PATH",
        str(tmp_path / "guardian_cache.json"),
    )
    monkeypatch.setenv(
        "COMPETITOR_TRACKER_GUARDIAN_BUDGET_PATH",
        str(tmp_path / "guardian_budget.json"),
    )
    monkeypatch.setenv("COMPETITOR_TRACKER_GUARDIAN_DAILY_REQUEST_LIMIT", "1")
    monkeypatch.setenv("COMPETITOR_TRACKER_GUARDIAN_CACHE_TTL_SECONDS", "0")
    payload = {
        "response": {
            "status": "ok",
            "results": [
                {
                    "webTitle": "Uber expands airport rides in Mexico City",
                    "webUrl": "https://www.theguardian.com/world/2026/may/18/uber-mexico-city",
                    "webPublicationDate": "2026-05-18T09:00:00Z",
                    "fields": {"trailText": "Expansion update from Mexico City."},
                }
            ],
        }
    }
    provider = GuardianProvider(
        session=FakeSession([FakeResponse(payload=payload)]),
        api_key="guardian-test-key",
    )

    provider.fetch(
        ProviderRequest(
            competitors=("Uber",),
            days=7,
            queries=['"Uber" launch Mexico'],
        )
    )

    with pytest.raises(ProviderError, match="daily request limit reached") as exc_info:
        provider.fetch(
            ProviderRequest(
                competitors=("Uber",),
                days=7,
                queries=['"Uber" expansion Mexico'],
            )
        )

    assert exc_info.value.diagnostics["queries"][0]["budget_hit"] is True


def test_regional_rss_provider_fetches_curated_feed_articles():
    xml_payload = b"""
    <rss>
      <channel>
        <item>
          <title>Grab rolls out new airport pricing program in Manila</title>
          <link>https://example.com/grab-manila-pricing</link>
          <pubDate>Tue, 18 May 2026 09:00:00 GMT</pubDate>
          <description><![CDATA[<p>GrabTaxi launches a new airport discount.</p>]]></description>
        </item>
      </channel>
    </rss>
    """
    provider = RegionalRssProvider(
        session=FakeSession([FakeResponse(content=xml_payload)]),
        feeds_by_region={
            "sea": [
                type(
                    "Feed",
                    (),
                    {
                        "name": "CNA Asia",
                        "url": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6511",
                        "language": "en",
                    },
                )()
            ]
        },
        competitor_aliases={"Grab": ("GrabTaxi",)},
    )

    articles, diagnostics = provider.fetch_with_diagnostics(
        ProviderRequest(
            competitors=("Grab", "Gojek"),
            days=7,
            queries=[],
            regions=("sea",),
        )
    )

    assert len(articles) == 1
    assert articles[0].provider == "regional_rss"
    assert articles[0].region == "sea"
    assert articles[0].competitor_hints == ("Grab",)
    assert articles[0].metadata["source_tier"] == "tier2_direct"
    assert diagnostics["items_after_filter"] == 1


def test_regional_rss_provider_avoids_false_positive_for_short_numeric_competitor():
    xml_payload = b"""
    <rss>
      <channel>
        <item>
          <title>Inflation reaches 99% in Brazil amid transport price pressure</title>
          <link>https://example.com/inflation-brazil</link>
          <pubDate>Tue, 18 May 2026 09:00:00 GMT</pubDate>
          <description><![CDATA[<p>Macro update with no ride-hailing company mention.</p>]]></description>
        </item>
      </channel>
    </rss>
    """
    provider = RegionalRssProvider(
        session=FakeSession([FakeResponse(content=xml_payload)]),
        feeds_by_region={
            "latam": [
                type(
                    "Feed",
                    (),
                    {
                        "name": "Brazil Business News",
                        "url": "https://example.com/rss.xml",
                        "language": "pt",
                    },
                )()
            ]
        },
    )

    articles, diagnostics = provider.fetch_with_diagnostics(
        ProviderRequest(
            competitors=("99",),
            days=7,
            queries=[],
            regions=("latam",),
        )
    )

    assert articles == []
    assert diagnostics["items_after_filter"] == 0


def test_regional_rss_provider_matches_short_numeric_competitor_with_context():
    xml_payload = b"""
    <rss>
      <channel>
        <item>
          <title>Brazil's 99 app expands electric ride option in Sao Paulo</title>
          <link>https://example.com/99-electric-rides</link>
          <pubDate>Tue, 18 May 2026 09:00:00 GMT</pubDate>
          <description><![CDATA[<p>The ride-hailing app 99 is adding more EV drivers.</p>]]></description>
        </item>
      </channel>
    </rss>
    """
    provider = RegionalRssProvider(
        session=FakeSession([FakeResponse(content=xml_payload)]),
        feeds_by_region={
            "latam": [
                type(
                    "Feed",
                    (),
                    {
                        "name": "Brazil Mobility",
                        "url": "https://example.com/rss.xml",
                        "language": "pt",
                    },
                )()
            ]
        },
    )

    articles, diagnostics = provider.fetch_with_diagnostics(
        ProviderRequest(
            competitors=("99",),
            days=7,
            queries=[],
            regions=("latam",),
        )
    )

    assert len(articles) == 1
    assert articles[0].competitor_hints == ("99",)
    assert diagnostics["items_after_filter"] == 1


def test_regional_rss_provider_uses_boundary_aware_competitor_matching():
    xml_payload = b"""
    <rss>
      <channel>
        <item>
          <title>Tourists grabbed airport taxis after the concert in Manila</title>
          <link>https://example.com/grabbed-taxis</link>
          <pubDate>Tue, 18 May 2026 09:00:00 GMT</pubDate>
          <description><![CDATA[<p>No company announcement was mentioned.</p>]]></description>
        </item>
      </channel>
    </rss>
    """
    provider = RegionalRssProvider(
        session=FakeSession([FakeResponse(content=xml_payload)]),
        feeds_by_region={
            "sea": [
                type(
                    "Feed",
                    (),
                    {
                        "name": "CNA Asia",
                        "url": "https://example.com/rss.xml",
                        "language": "en",
                    },
                )()
            ]
        },
        competitor_aliases={"Grab": ("GrabTaxi",)},
    )

    articles, diagnostics = provider.fetch_with_diagnostics(
        ProviderRequest(
            competitors=("Grab",),
            days=7,
            queries=[],
            regions=("sea",),
        )
    )

    assert articles == []
    assert diagnostics["items_after_filter"] == 0


def test_google_news_rss_provider_surfaces_request_diagnostics_on_http_failure():
    provider = GoogleNewsRssProvider(session=FakeSession([FakeResponse(status_code=429)]))

    with pytest.raises(ProviderError) as exc_info:
        provider.fetch_with_diagnostics(
            ProviderRequest(
                competitors=("Grab",),
                days=7,
                queries=['"Grab" launch Philippines'],
            )
        )

    diagnostics = exc_info.value.diagnostics
    assert diagnostics["provider"] == "google_news_rss"
    assert diagnostics["status"] == "error"
    assert diagnostics["queries"][0]["query"] == '"Grab" launch Philippines'
    assert diagnostics["queries"][0]["http_status"] == 429
    assert diagnostics["queries"][0]["items_found"] == 0
    assert diagnostics["queries"][0]["items_after_filter"] == 0
    assert "http 429" in diagnostics["queries"][0]["exception"]


def test_collect_raw_articles_generates_queries_and_persists_to_sqlite(tmp_path):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico"],
                    "language_hints": ["es"],
                }
            },
            "competitors_by_region": {"latam": ["Uber"]},
            "topic_groups": {"product_launch": ["launch"]},
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "daily_digest_limit": 10,
            "enabled_providers": ["google_news_rss"],
        }
    )
    runtime = TrackerRuntimeConfig(
        output_dir=tmp_path / "output",
        database_path=tmp_path / "tracker.db",
        lookback_days=7,
        min_score=5,
        config_path=tmp_path / "config.json",
    )

    (
        raw_articles,
        provider_names,
        provider_errors,
        fetched_articles_count,
        provider_diagnostics,
    ) = collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=("latam",),
        competitors=("Uber",),
        providers=[FakeProvider()],
    )

    assert len(raw_articles) == 1
    assert provider_names == ("fake_provider",)
    assert provider_errors == {}
    assert fetched_articles_count == 2
    assert provider_diagnostics["fake_provider"]["items_found"] == 2
    assert provider_diagnostics["fake_provider"]["items_after_global_dedup"] == 1
    assert raw_articles[0].query == '"Uber" product launch Latin America'
    assert raw_articles[0].competitor_hints == ("Uber",)

    with sqlite3.connect(runtime.database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM articles_raw").fetchone()[0]
        stored_query = connection.execute(
            "SELECT query_text FROM articles_raw LIMIT 1"
        ).fetchone()[0]
        stored_hints = connection.execute(
            "SELECT competitor_hints_json FROM articles_raw LIMIT 1"
        ).fetchone()[0]

    assert count == 1
    assert stored_query == '"Uber" product launch Latin America'
    assert stored_hints == '["Uber"]'


def test_collect_raw_articles_skips_newsapi_in_full_run_by_default(tmp_path):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico"],
                    "language_hints": ["es"],
                }
            },
            "competitors_by_region": {"latam": ["Uber"]},
            "topic_groups": {"product_launch": ["launch"]},
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "daily_digest_limit": 10,
            "enabled_providers": ["newsapi"],
        }
    )
    runtime = TrackerRuntimeConfig(
        output_dir=tmp_path / "output",
        database_path=tmp_path / "tracker.db",
        lookback_days=7,
        min_score=5,
        config_path=tmp_path / "config.json",
    )
    provider = NewsApiProvider(session=FakeSession([]), api_key="test-key")

    (
        raw_articles,
        provider_names,
        provider_errors,
        fetched_articles_count,
        provider_diagnostics,
    ) = collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=("latam",),
        competitors=("Uber",),
        providers=[provider],
    )

    assert raw_articles == []
    assert provider_names == ("newsapi",)
    assert fetched_articles_count == 0
    assert "disabled for full pipeline runs by default" in provider_errors["newsapi"]
    assert provider_diagnostics["newsapi"]["status"] == "skipped"
    assert provider.session.calls == []


def test_collect_raw_articles_limits_newsapi_query_count_per_run(tmp_path):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico"],
                    "language_hints": ["es"],
                }
            },
            "competitors_by_region": {"latam": ["Uber", "DiDi"]},
            "topic_groups": {
                "product_launch": ["launch"],
                "campaign_launches": ["campaign"],
            },
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "daily_digest_limit": 10,
            "enabled_providers": ["newsapi"],
        }
    )
    runtime = TrackerRuntimeConfig(
        output_dir=tmp_path / "output",
        database_path=tmp_path / "tracker.db",
        lookback_days=7,
        min_score=5,
        config_path=tmp_path / "config.json",
        enable_newsapi_full_run=True,
        newsapi_max_queries_per_run=1,
    )
    captured = {}

    class FakeNewsApiProvider:
        name = "newsapi"

        def fetch_with_diagnostics(self, request: ProviderRequest):
            captured["queries"] = list(request.queries)
            return [], {
                "provider": self.name,
                "status": "ok",
                "queries": [],
                "items_found": 0,
                "items_after_filter": 0,
                "items_after_global_dedup": 0,
            }

    (
        raw_articles,
        provider_names,
        provider_errors,
        fetched_articles_count,
        provider_diagnostics,
    ) = collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=("latam",),
        competitors=("Uber", "DiDi"),
        providers=[FakeNewsApiProvider()],
    )

    assert raw_articles == []
    assert provider_names == ("newsapi",)
    assert provider_errors == {}
    assert fetched_articles_count == 0
    assert len(captured["queries"]) == 1
    assert captured["queries"] == ['"Uber" product launch Latin America']
    assert "warning" in provider_diagnostics["newsapi"]


def test_collect_raw_articles_simplifies_gdelt_queries_for_broad_source(tmp_path):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "sea": {
                    "label": "Southeast Asia",
                    "geo_terms": ["Indonesia", "Thailand"],
                    "language_hints": ["en"],
                }
            },
            "competitors_by_region": {"sea": ["Grab"]},
            "topic_groups": {
                "market_expansion": ["launch", "new city", "market entry"],
                "pricing_promo": ["discount", "promo code"],
            },
            "topic_priority_groups": ["market_expansion", "pricing_promo"],
            "keyword_templates": [
                '"{competitor}" {topic_keywords} {geo_terms}',
                '"{competitor}" {topic_name} {region_label}',
            ],
            "daily_digest_limit": 10,
            "enabled_providers": ["gdelt"],
        }
    )
    runtime = TrackerRuntimeConfig(
        output_dir=tmp_path / "output",
        database_path=tmp_path / "tracker.db",
        lookback_days=7,
        min_score=5,
        config_path=tmp_path / "config.json",
    )
    captured = {}

    class FakeGdeltProvider:
        name = "gdelt"

        def fetch_with_diagnostics(self, request: ProviderRequest):
            captured["queries"] = list(request.queries)
            return [], {
                "provider": self.name,
                "status": "ok",
                "queries": [],
                "items_found": 0,
                "items_after_filter": 0,
                "items_after_global_dedup": 0,
            }

    collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=("sea",),
        competitors=("Grab",),
        providers=[FakeGdeltProvider()],
    )

    assert captured["queries"] == [
        '"Grab" launch Indonesia',
        '"Grab" discount Indonesia',
    ]


def test_collect_raw_articles_relaxes_google_news_queries_for_secondary_source(tmp_path):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "sea": {
                    "label": "Southeast Asia",
                    "geo_terms": ["Indonesia", "Thailand"],
                    "language_hints": ["en"],
                }
            },
            "competitors_by_region": {"sea": ["Grab"]},
            "topic_groups": {
                "market_expansion": ["launch", "new city", "market entry"],
                "pricing_promo": ["discount", "promo code"],
            },
            "topic_priority_groups": ["market_expansion", "pricing_promo"],
            "keyword_templates": [
                '"{competitor}" {topic_keywords} {geo_terms}',
                '"{competitor}" {topic_name} {region_label} {language_hints}',
            ],
            "daily_digest_limit": 10,
            "enabled_providers": ["google_news_rss"],
        }
    )
    runtime = TrackerRuntimeConfig(
        output_dir=tmp_path / "output",
        database_path=tmp_path / "tracker.db",
        lookback_days=7,
        min_score=5,
        config_path=tmp_path / "config.json",
    )
    captured = {}

    class FakeGoogleNewsProvider:
        name = "google_news_rss"

        def fetch_with_diagnostics(self, request: ProviderRequest):
            captured["queries"] = list(request.queries)
            return [], {
                "provider": self.name,
                "status": "ok",
                "queries": [],
                "items_found": 0,
                "items_after_filter": 0,
                "items_after_global_dedup": 0,
            }

    collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=("sea",),
        competitors=("Grab",),
        providers=[FakeGoogleNewsProvider()],
    )

    assert captured["queries"] == [
        '"Grab" launch Indonesia',
        '"Grab" discount Indonesia',
    ]


def test_collect_raw_articles_limits_guardian_query_count_and_prefers_historical_precision(tmp_path):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico"],
                    "language_hints": ["es"],
                }
            },
            "competitors_by_region": {"latam": ["Uber", "DiDi"]},
            "topic_groups": {
                "market_expansion": ["launch"],
            },
            "topic_priority_groups": ["market_expansion"],
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "daily_digest_limit": 10,
            "enabled_providers": ["guardian"],
        }
    )
    runtime = TrackerRuntimeConfig(
        output_dir=tmp_path / "output",
        database_path=tmp_path / "tracker.db",
        lookback_days=7,
        min_score=5,
        config_path=tmp_path / "config.json",
        guardian_max_queries_per_run=1,
    )

    storage = SQLiteTrackerStorage(runtime.database_path)
    strong_raw = RawArticle(
        title="Uber launches premium airport rides in Mexico City",
        url="https://example.com/uber-strong",
        provider="guardian",
        source="The Guardian",
        published_at="2026-05-18T09:00:00Z",
        snippet="Expansion update.",
        query='"Uber" market expansion Latin America',
        region="latam",
        language="en",
        competitor_hints=("Uber",),
    )
    weak_raw = RawArticle(
        title="DiDi mentioned in roundup",
        url="https://example.com/didi-weak",
        provider="guardian",
        source="The Guardian",
        published_at="2026-05-18T09:00:00Z",
        snippet="Roundup mention.",
        query='"DiDi" market expansion Latin America',
        region="latam",
        language="en",
        competitor_hints=("DiDi",),
    )
    strong_candidate = CandidateArticle(
        raw_article=strong_raw,
        competitor="Uber",
        topic_group="market_expansion",
        score=9,
        matched_keywords=("launch",),
        summary="Uber launch signal.",
        region="latam",
        language_hint="en",
        reasons=("high precision",),
    )

    storage.insert_raw_article(strong_raw)
    storage.insert_candidate(strong_candidate)
    storage.insert_alert(strong_candidate.to_alert())
    storage.insert_raw_article(weak_raw)

    captured = {}

    class FakeGuardianProvider:
        name = "guardian"

        def fetch_with_diagnostics(self, request: ProviderRequest):
            captured["queries"] = list(request.queries)
            return [], {
                "provider": self.name,
                "status": "ok",
                "queries": [],
                "items_found": 0,
                "items_after_filter": 0,
                "items_after_global_dedup": 0,
            }

    (
        raw_articles,
        provider_names,
        provider_errors,
        fetched_articles_count,
        provider_diagnostics,
    ) = collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=("latam",),
        competitors=("Uber", "DiDi"),
        providers=[FakeGuardianProvider()],
    )

    assert raw_articles == []
    assert provider_names == ("guardian",)
    assert provider_errors == {}
    assert fetched_articles_count == 0
    assert captured["queries"] == ['"Uber" market expansion Latin America']
    assert "warning" in provider_diagnostics["guardian"]


def test_collect_raw_articles_uses_focused_queries_for_guardian_quality_source(tmp_path):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "latam": {
                    "label": "Latin America",
                    "geo_terms": ["Mexico", "Brazil"],
                    "language_hints": ["en"],
                }
            },
            "competitors_by_region": {"latam": ["Uber"]},
            "topic_groups": {
                "market_expansion": ["launch", "new city", "market entry"],
                "pricing_promo": ["discount", "promo code"],
            },
            "topic_priority_groups": ["market_expansion", "pricing_promo"],
            "keyword_templates": [
                '"{competitor}" {topic_keywords} {geo_terms}',
                '"{competitor}" {topic_name} {region_label}',
            ],
            "daily_digest_limit": 10,
            "enabled_providers": ["guardian"],
        }
    )
    runtime = TrackerRuntimeConfig(
        output_dir=tmp_path / "output",
        database_path=tmp_path / "tracker.db",
        lookback_days=7,
        min_score=5,
        config_path=tmp_path / "config.json",
        guardian_max_queries_per_run=10,
    )
    captured = {}

    class FakeGuardianProvider:
        name = "guardian"

        def fetch_with_diagnostics(self, request: ProviderRequest):
            captured["queries"] = list(request.queries)
            return [], {
                "provider": self.name,
                "status": "ok",
                "queries": [],
                "items_found": 0,
                "items_after_filter": 0,
                "items_after_global_dedup": 0,
            }

    collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=("latam",),
        competitors=("Uber",),
        providers=[FakeGuardianProvider()],
    )

    assert captured["queries"] == [
        '"Uber" market expansion Latin America',
        '"Uber" pricing promo Latin America',
    ]


def test_collect_raw_articles_exposes_cache_and_source_tier_metrics(tmp_path):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "sea": {
                    "label": "Southeast Asia",
                    "geo_terms": ["Philippines"],
                    "language_hints": ["en"],
                }
            },
            "competitors_by_region": {"sea": ["Grab"]},
            "topic_groups": {"pricing_promo": ["discount"]},
            "topic_priority_groups": ["pricing_promo"],
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "daily_digest_limit": 10,
            "enabled_providers": ["regional_rss", "guardian"],
        }
    )
    runtime = TrackerRuntimeConfig(
        output_dir=tmp_path / "output",
        database_path=tmp_path / "tracker.db",
        lookback_days=7,
        min_score=5,
        config_path=tmp_path / "config.json",
        guardian_max_queries_per_run=1,
    )

    direct = RawArticle(
        title="Grab rolls out new airport pricing program in Manila",
        url="https://example.com/direct-grab",
        provider="regional_rss",
        source="CNA Asia",
        published_at="2026-05-18T09:00:00Z",
        snippet="Direct source report.",
        query='"Grab" pricing promo Southeast Asia',
        region="sea",
        language="en",
        competitor_hints=("Grab",),
        metadata={"source_tier": "tier2_direct"},
    )
    aggregate = RawArticle(
        title="Grab rolls out new airport pricing program in Manila | mirror",
        url="https://mirror.example.com/grab",
        provider="guardian",
        source="Mirror Site",
        published_at="2026-05-18T09:00:00Z",
        snippet="Aggregated mirror.",
        query='"Grab" pricing promo Southeast Asia',
        region="sea",
        language="en",
        competitor_hints=("Grab",),
        metadata={"source_tier": "tier1_aggregator"},
    )

    class FakeRegionalRssProvider:
        name = "regional_rss"

        def fetch_with_diagnostics(self, request: ProviderRequest):
            return [direct], {
                "provider": self.name,
                "status": "ok",
                "queries": [
                    {
                        "provider": self.name,
                        "query": "",
                        "request_url": "https://example.com/feed.xml",
                        "http_status": 200,
                        "exception": "",
                        "items_found": 1,
                        "items_after_filter": 1,
                        "status": "cached",
                    }
                ],
                "items_found": 1,
                "items_after_filter": 1,
                "items_after_global_dedup": 0,
                "feeds_skipped": 2,
            }

    class FakeGuardianProvider:
        name = "guardian"

        def fetch_with_diagnostics(self, request: ProviderRequest):
            return [aggregate], {
                "provider": self.name,
                "status": "ok",
                "queries": [
                    {
                        "provider": self.name,
                        "query": request.queries[0],
                        "request_url": "https://content.guardianapis.com/search",
                        "http_status": 200,
                        "exception": "",
                        "items_found": 1,
                        "items_after_filter": 1,
                        "status": "ok",
                    }
                ],
                "items_found": 1,
                "items_after_filter": 1,
                "items_after_global_dedup": 0,
            }

    (
        raw_articles,
        _provider_names,
        _provider_errors,
        fetched_articles_count,
        provider_diagnostics,
    ) = collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=("sea",),
        competitors=("Grab",),
        providers=[FakeGuardianProvider(), FakeRegionalRssProvider()],
    )

    assert len(raw_articles) == 1
    assert raw_articles[0].provider == "regional_rss"
    assert fetched_articles_count == 2
    assert provider_diagnostics["regional_rss"]["source_tier_wins"] == 1
    assert provider_diagnostics["regional_rss"]["items_after_global_dedup"] == 1
    assert provider_diagnostics["guardian"]["items_after_global_dedup"] == 0
    assert provider_diagnostics["global_dedup"]["source_tier_wins"] == 1


def test_extract_provider_metrics_builds_run_summary_friendly_kpis():
    metrics = _extract_provider_metrics(
        {
            "regional_rss": {
                "queries": [
                    {"status": "cached"},
                    {"status": "skipped", "budget_hit": True, "cooldown_hit": True},
                ],
                "source_tier_wins": 2,
                "items_after_global_dedup": 3,
                "feeds_skipped": 4,
            },
            "global_dedup": {
                "queries": [],
                "source_tier_wins": 1,
                "items_after_global_dedup": 5,
            },
        }
    )

    assert metrics["regional_rss"] == {
        "cache_hits": 1,
        "skipped_items": 1,
        "budget_hits": 1,
        "cooldown_hits": 1,
        "source_tier_wins": 2,
        "items_after_global_dedup": 3,
        "feeds_skipped": 4,
    }
    assert metrics["global_dedup"] == {
        "cache_hits": 0,
        "skipped_items": 0,
        "budget_hits": 0,
        "cooldown_hits": 0,
        "source_tier_wins": 1,
        "items_after_global_dedup": 5,
    }


def test_collect_raw_articles_configures_regional_rss_provider_from_config(tmp_path):
    config = TrackerConfig.from_dict(
        {
            "regions": {
                "sea": {
                    "label": "Southeast Asia",
                    "geo_terms": ["Philippines"],
                    "language_hints": ["en"],
                }
            },
            "competitors_by_region": {"sea": ["Grab"]},
            "topic_groups": {"pricing_promo": ["discount"]},
            "keyword_templates": ['"{competitor}" {topic_name} {region_label}'],
            "daily_digest_limit": 10,
            "enabled_providers": ["regional_rss"],
            "regional_rss_feeds": {
                "sea": [
                    {
                        "name": "CNA Asia",
                        "url": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6511",
                        "language": "en",
                    }
                ]
            },
            "competitor_aliases": {
                "Grab": ["GrabTaxi"],
            },
        }
    )
    runtime = TrackerRuntimeConfig(
        output_dir=tmp_path / "output",
        database_path=tmp_path / "tracker.db",
        lookback_days=7,
        min_score=5,
        config_path=tmp_path / "config.json",
    )
    provider = RegionalRssProvider(session=FakeSession([]))

    collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=("sea",),
        competitors=("Grab",),
        providers=[provider],
    )

    assert "sea" in provider.feeds_by_region
    assert provider.competitor_aliases["Grab"] == ("GrabTaxi",)


def test_newsapi_provider_uses_local_cache_for_repeated_query(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "COMPETITOR_TRACKER_NEWSAPI_CACHE_PATH",
        str(tmp_path / "newsapi_cache.json"),
    )
    monkeypatch.setenv(
        "COMPETITOR_TRACKER_NEWSAPI_BUDGET_PATH",
        str(tmp_path / "newsapi_budget.json"),
    )
    monkeypatch.setenv("COMPETITOR_TRACKER_NEWSAPI_CACHE_TTL_SECONDS", "600")
    payload = {
        "status": "ok",
        "articles": [
            {
                "title": "Uber launches airport rides in Mexico City",
                "url": "https://example.com/uber-airport-newsapi",
                "publishedAt": "2026-05-18T09:00:00Z",
                "description": "Airport expansion update.",
                "source": {"name": "Example News"},
            }
        ],
    }
    session = FakeSession([FakeResponse(payload=payload)])
    provider = NewsApiProvider(session=session, api_key="test-key")
    request = ProviderRequest(
        competitors=("Uber",),
        days=7,
        queries=['"Uber" launch Mexico'],
    )

    provider.fetch_with_diagnostics(request)
    articles, diagnostics = provider.fetch_with_diagnostics(request)

    assert len(session.calls) == 1
    assert len(articles) == 1
    assert diagnostics["queries"][0]["status"] == "cached"
    assert diagnostics["queries"][0]["cached"] is True


def test_newsapi_provider_stops_when_daily_budget_is_exhausted(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "COMPETITOR_TRACKER_NEWSAPI_CACHE_PATH",
        str(tmp_path / "newsapi_cache.json"),
    )
    monkeypatch.setenv(
        "COMPETITOR_TRACKER_NEWSAPI_BUDGET_PATH",
        str(tmp_path / "newsapi_budget.json"),
    )
    monkeypatch.setenv("COMPETITOR_TRACKER_NEWSAPI_DAILY_REQUEST_LIMIT", "1")
    monkeypatch.setenv("COMPETITOR_TRACKER_NEWSAPI_CACHE_TTL_SECONDS", "0")
    payload = {
        "status": "ok",
        "articles": [
            {
                "title": "Uber launches airport rides in Mexico City",
                "url": "https://example.com/uber-airport-newsapi",
                "publishedAt": "2026-05-18T09:00:00Z",
                "description": "Airport expansion update.",
                "source": {"name": "Example News"},
            }
        ],
    }
    provider = NewsApiProvider(
        session=FakeSession([FakeResponse(payload=payload)]),
        api_key="test-key",
    )

    provider.fetch(
        ProviderRequest(
            competitors=("Uber",),
            days=7,
            queries=['"Uber" launch Mexico'],
        )
    )

    with pytest.raises(ProviderError, match="daily request limit reached") as exc_info:
        provider.fetch(
            ProviderRequest(
                competitors=("Uber",),
                days=7,
                queries=['"Uber" expansion Mexico'],
            )
        )

    assert exc_info.value.diagnostics["queries"][0]["budget_hit"] is True


def test_build_providers_warns_and_surfaces_unknown_provider(caplog):
    providers = build_providers(["google_news_rss", "mystery_provider"])

    assert supported_provider_names() == (
        "newsapi",
        "gdelt",
        "google_news_rss",
        "guardian",
        "regional_rss",
    )
    assert [provider.name for provider in providers] == [
        "google_news_rss",
        "mystery_provider",
    ]
    assert "Unknown provider 'mystery_provider' is enabled in config" in caplog.text

    with pytest.raises(
        ProviderError,
        match="enabled in config but is not supported",
    ):
        providers[1].fetch(
            ProviderRequest(
                competitors=("Uber",),
                days=7,
                queries=['"Uber" launch Mexico'],
            )
        )


def test_provider_request_can_resolve_query_specific_competitor_hints():
    request = ProviderRequest(
        competitors=("Uber", "DiDi"),
        days=7,
        queries=['"Uber" launch Mexico', '"DiDi" launch Mexico'],
        query_competitor_hints={
            '"Uber" launch Mexico': ("Uber",),
            '"DiDi" launch Mexico': ("DiDi",),
        },
    )

    assert request.competitor_hints_for_query('"Uber" launch Mexico') == ("Uber",)
    assert request.competitor_hints_for_query('"DiDi" launch Mexico') == ("DiDi",)
    assert request.competitor_hints_for_query('"Cabify" launch Mexico') == (
        "Uber",
        "DiDi",
    )
