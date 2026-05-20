from competitor_tracker.models import RawArticle
from competitor_tracker.normalization import (
    deduplicate_raw_articles,
    normalize_raw_article,
    normalize_source,
    normalize_title,
    normalize_url,
    parse_published_at,
)


def test_normalize_url_removes_tracking_params_and_www():
    normalized = normalize_url(
        "https://www.example.com/story/?utm_source=x&b=2&a=1&fbclid=123"
    )

    assert normalized == "https://example.com/story?a=1&b=2"


def test_normalize_title_matches_legacy_style_cleanup():
    title = "inDrive expands delivery service in Mexico City | Another Publisher"

    assert normalize_title(title) == "indrive expands delivery service in mexico city"


def test_parse_published_at_supports_iso_and_rss_dates():
    assert parse_published_at("2026-05-18T09:00:00Z") == "2026-05-18"
    assert parse_published_at("Tue, 18 May 2026 09:00:00 GMT") == "2026-05-18"


def test_normalize_raw_article_applies_url_source_and_date_cleanup():
    article = RawArticle(
        title="  Uber expands in Mexico  ",
        url="https://www.example.com/news/uber?utm_source=x",
        provider="newsapi",
        source="",
        published_at="2026-05-18T09:00:00Z",
        snippet="  Big   launch   ",
        query="  Uber   launch Mexico ",
    )

    normalized = normalize_raw_article(article)

    assert normalized.title == "Uber expands in Mexico"
    assert normalized.url == "https://example.com/news/uber"
    assert normalized.source == "example.com"
    assert normalized.published_at == "2026-05-18"
    assert normalized.snippet == "Big launch"
    assert normalized.query == "Uber launch Mexico"


def test_deduplicate_raw_articles_removes_url_and_title_duplicates():
    articles = [
        RawArticle(
            title="Uber expands delivery service in Mexico City - Tech News",
            url="https://example.com/one?utm_source=x",
            provider="newsapi",
            source="Tech News",
        ),
        RawArticle(
            title="Uber expands delivery service in Mexico City | Another Publisher",
            url="https://example.com/two",
            provider="gdelt",
            source="Another Publisher",
        ),
        RawArticle(
            title="Different title same URL",
            url="https://example.com/one",
            provider="google_news_rss",
            source="Example",
        ),
    ]

    unique = deduplicate_raw_articles(articles)

    assert len(unique) == 1
    assert unique[0].url == "https://example.com/one"


def test_normalize_source_falls_back_to_domain():
    assert normalize_source("", "https://www.example.com/news") == "example.com"
