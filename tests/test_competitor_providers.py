import sqlite3

from competitor_tracker.cli import collect_raw_articles
from competitor_tracker.config import TrackerConfig, TrackerRuntimeConfig
from competitor_tracker.models import RawArticle
from competitor_tracker.providers import GdeltProvider, GoogleNewsRssProvider, ProviderRequest


class FakeResponse:
    def __init__(self, *, content: bytes = b"", payload: dict | None = None) -> None:
        self.content = content
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        return None

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
                competitor_hints=tuple(request.competitors),
            ),
            RawArticle(
                title="Uber launches airport rides in Mexico City | Another Publisher",
                url="https://example.com/uber-airport-copy",
                provider=self.name,
                source="Another Publisher",
                published_at="Tue, 18 May 2026 09:00:00 GMT",
                snippet="Airport expansion copy.",
                query=request.queries[0],
                competitor_hints=tuple(request.competitors),
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

    articles = provider.fetch(
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

    articles = provider.fetch(
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

    raw_articles, provider_names, provider_errors = collect_raw_articles(
        config=config,
        runtime=runtime,
        regions=("latam",),
        competitors=("Uber",),
        providers=[FakeProvider()],
    )

    assert len(raw_articles) == 1
    assert provider_names == ("fake_provider",)
    assert provider_errors == {}
    assert raw_articles[0].query == '"Uber" product launch Latin America'

    with sqlite3.connect(runtime.database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM articles_raw").fetchone()[0]
        stored_query = connection.execute(
            "SELECT query_text FROM articles_raw LIMIT 1"
        ).fetchone()[0]

    assert count == 1
    assert stored_query == '"Uber" product launch Latin America'
