import sqlite3

import pytest

from competitor_tracker.cli import collect_raw_articles
from competitor_tracker.config import TrackerConfig, TrackerRuntimeConfig
from competitor_tracker.models import RawArticle
from competitor_tracker.providers import (
    GdeltProvider,
    GoogleNewsRssProvider,
    NewsApiProvider,
    ProviderError,
    ProviderRequest,
    build_providers,
    supported_provider_names,
)


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

    def get(self, *args, **kwargs):
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


def test_newsapi_provider_fetches_raw_articles():
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


def test_build_providers_warns_and_surfaces_unknown_provider(caplog):
    providers = build_providers(["google_news_rss", "mystery_provider"])

    assert supported_provider_names() == ("newsapi", "gdelt", "google_news_rss")
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
