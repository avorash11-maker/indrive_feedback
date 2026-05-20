from dataclasses import asdict

from competitor_tracker.analyzer import CompetitorAnalyzer
from competitor_tracker.digest import DigestBuilder
from competitor_tracker.models import (
    CandidateArticle,
    DeliveryRecord,
    RawArticle,
    RunSummary,
)


def test_candidate_article_exposes_raw_article_fields_and_builds_alert():
    raw_article = RawArticle(
        title="Uber launches new driver insurance program",
        url="https://example.com/uber-insurance",
        provider="newsapi",
        source="Example News",
        region="latam",
        language="en",
    )
    candidate = CandidateArticle(
        raw_article=raw_article,
        competitor="Uber",
        topic_group="safety",
        score=8,
        matched_keywords=("insurance", "driver"),
        reasons=("high-signal product update",),
    )

    alert = candidate.to_alert(severity="high", channels=("email", "email", "slack"))

    assert candidate.title == "Uber launches new driver insurance program"
    assert candidate.url == "https://example.com/uber-insurance"
    assert alert.competitor == "Uber"
    assert alert.severity == "high"
    assert alert.priority == "MEDIUM"
    assert alert.delivery_channels == ("email", "slack")
    assert alert.candidate is candidate


def test_analyzer_filters_candidate_articles_by_score():
    analyzer = CompetitorAnalyzer(min_score=6)
    raw_article = RawArticle(
        title="Bolt expands in Kenya",
        url="https://example.com/bolt-kenya",
        provider="gdelt",
    )
    kept = CandidateArticle(
        raw_article=raw_article,
        competitor="Bolt",
        topic_group="product_launch",
        score=7,
    )
    dropped = CandidateArticle(
        raw_article=raw_article,
        competitor="Bolt",
        topic_group="pricing",
        score=4,
    )

    result = analyzer.analyze([kept, dropped])

    assert result.candidates == [kept]
    assert result.dropped_count == 1


def test_digest_builder_converts_candidates_into_alerts():
    raw_article = RawArticle(
        title="Grab pilots new subscription tier",
        url="https://example.com/grab-tier",
        provider="google_news_rss",
    )
    candidate = CandidateArticle(
        raw_article=raw_article,
        competitor="Grab",
        topic_group="product_launch",
        score=9,
        summary="New paid tier launched in Singapore.",
    )

    digest = DigestBuilder().build(
        competitors=["Grab"],
        candidates=[candidate],
        regions=["sea"],
    )

    assert digest.competitors == ("Grab",)
    assert digest.regions == ("sea",)
    assert len(digest.alerts) == 1
    assert digest.alerts[0].headline == "Grab: Grab pilots new subscription tier"
    assert digest.highlights == ("Grab: Grab pilots new subscription tier",)


def test_run_summary_and_delivery_record_are_serializable():
    summary = RunSummary(
        started_at="2026-05-18T10:00:00Z",
        finished_at="2026-05-18T10:05:00Z",
        regions=("sea", "latam"),
        providers=("newsapi", "gdelt"),
        queries_generated=18,
        raw_articles_collected=45,
        candidates_kept=7,
        alerts_created=4,
        daily_digest_limit=12,
        provider_errors={"gdelt": "timeout"},
    )
    delivery = DeliveryRecord(
        alert_key="grab::product_launch::https://example.com/grab-tier",
        channel="email",
        status="pending",
        destination="digest@example.com",
    )

    summary_payload = asdict(summary)
    delivery_payload = asdict(delivery)

    assert summary_payload["providers"] == ("newsapi", "gdelt")
    assert summary_payload["provider_errors"] == {"gdelt": "timeout"}
    assert delivery_payload["status"] == "pending"
    assert delivery_payload["destination"] == "digest@example.com"
