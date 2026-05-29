from datetime import date

from competitor_tracker.digest import DigestBuilder
from competitor_tracker.models import CandidateArticle, DeliveryRecord, RawArticle
from competitor_tracker.storage import SQLiteTrackerStorage


def build_candidate(
    *,
    title: str,
    url: str,
    competitor: str,
    topic_group: str,
    score: int,
    published_at: str,
    country_hint: str = "",
    region: str = "sea",
) -> CandidateArticle:
    return CandidateArticle(
        raw_article=RawArticle(
            title=title,
            url=url,
            provider="google_news_rss",
            source="Example News",
            published_at=published_at,
            snippet=title,
        ),
        competitor=competitor,
        topic_group=topic_group,
        score=score,
        region=region,
        country_hint=country_hint or None,
        language_hint="en",
        matched_keywords=("launch",),
        reasons=("priority_signal",),
        summary=title,
    )


def test_digest_ranks_priority_then_freshness_then_score():
    builder = DigestBuilder()
    lower_priority_fresher = build_candidate(
        title="Grab updates rider messaging",
        url="https://example.com/grab-msg",
        competitor="Grab",
        topic_group="pricing",
        score=7,
        published_at="2026-05-18",
        country_hint="Philippines",
    )
    high_priority_older = build_candidate(
        title="Grab launches major airport partnership",
        url="https://example.com/grab-airport",
        competitor="Grab",
        topic_group="product_launch",
        score=9,
        published_at="2026-05-17",
        country_hint="Philippines",
    )
    same_priority_lower_score = build_candidate(
        title="Grab launches city promo",
        url="https://example.com/grab-promo",
        competitor="Grab",
        topic_group="product_launch",
        score=9,
        published_at="2026-05-17",
        country_hint="Thailand",
    )

    digest = builder.build(
        competitors=["Grab"],
        candidates=[lower_priority_fresher, same_priority_lower_score, high_priority_older],
        regions=["sea"],
        digest_limit=10,
    )

    assert [alert.headline for alert in digest.alerts] == [
        "Grab: Grab launches city promo",
        "Grab: Grab launches major airport partnership",
        "Grab: Grab updates rider messaging",
    ]


def test_digest_limit_and_suppression_of_sent_and_similar_alerts(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")
    historical = build_candidate(
        title="Grab launches driver support in the Philippines",
        url="https://example.com/grab-historical",
        competitor="Grab",
        topic_group="product_launch",
        score=8,
        published_at="2026-05-17",
        country_hint="Philippines",
    ).to_alert()
    storage.insert_alert(historical)
    storage.log_delivery(
        DeliveryRecord(
            alert_key=historical.digest_key,
            channel="daily_digest",
            status="delivered",
            destination="ops@example.com",
            delivered_at="2026-05-17T09:00:00Z",
        )
    )

    already_sent = build_candidate(
        title="Grab launches driver support in the Philippines",
        url="https://example.com/grab-historical",
        competitor="Grab",
        topic_group="product_launch",
        score=8,
        published_at="2026-05-18",
        country_hint="Philippines",
    )
    similar_history = build_candidate(
        title="Grab launches driver support program in the Philippines | Another Publisher",
        url="https://example.com/grab-similar",
        competitor="Grab",
        topic_group="product_launch",
        score=9,
        published_at="2026-05-18",
        country_hint="Philippines",
    )
    unique = build_candidate(
        title="Gojek expands courier rewards in Indonesia",
        url="https://example.com/gojek-rewards",
        competitor="Gojek",
        topic_group="product_launch",
        score=9,
        published_at="2026-05-18",
        country_hint="Indonesia",
    )

    digest = DigestBuilder().build(
        competitors=["Grab", "Gojek"],
        candidates=[already_sent, similar_history, unique],
        regions=["sea"],
        digest_limit=2,
        storage=storage,
        delivery_channel="daily_digest",
        delivery_destination="ops@example.com",
    )

    assert len(digest.alerts) == 1
    assert digest.alerts[0].competitor == "Gojek"
    assert digest.alerts[0].headline == "Gojek: Gojek expands courier rewards in Indonesia"


def test_digest_history_suppression_ignores_dry_run_only_delivery_history(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")
    historical = build_candidate(
        title="Grab launches driver support in the Philippines",
        url="https://example.com/grab-historical",
        competitor="Grab",
        topic_group="product_launch",
        score=8,
        published_at="2026-05-17",
        country_hint="Philippines",
    ).to_alert()
    storage.insert_alert(historical)
    storage.log_delivery(
        DeliveryRecord(
            alert_key=historical.digest_key,
            channel="telegram",
            status="dry_run",
            destination="chat-1",
        )
    )

    rerun_candidate = build_candidate(
        title="Grab launches driver support program in the Philippines | Another Publisher",
        url="https://example.com/grab-similar",
        competitor="Grab",
        topic_group="product_launch",
        score=9,
        published_at="2026-05-18",
        country_hint="Philippines",
    )

    digest = DigestBuilder().build(
        competitors=["Grab"],
        candidates=[rerun_candidate],
        regions=["sea"],
        digest_limit=5,
        storage=storage,
        delivery_channel="telegram",
        delivery_destination="chat-1",
    )

    assert len(digest.alerts) == 1
    assert digest.alerts[0].headline == (
        "Grab: Grab launches driver support program in the Philippines | Another Publisher"
    )


def test_digest_history_suppression_ignores_deliveries_for_other_destination(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")
    historical = build_candidate(
        title="Grab launches driver support in the Philippines",
        url="https://example.com/grab-historical",
        competitor="Grab",
        topic_group="product_launch",
        score=8,
        published_at="2026-05-17",
        country_hint="Philippines",
    ).to_alert()
    storage.insert_alert(historical)
    storage.mark_delivered(
        alert_key=historical.digest_key,
        channel="telegram",
        delivered_at="2026-05-17T09:00:00Z",
        destination="chat-1",
    )

    rerun_candidate = build_candidate(
        title="Grab launches driver support program in the Philippines | Another Publisher",
        url="https://example.com/grab-similar",
        competitor="Grab",
        topic_group="product_launch",
        score=9,
        published_at="2026-05-18",
        country_hint="Philippines",
    )

    digest = DigestBuilder().build(
        competitors=["Grab"],
        candidates=[rerun_candidate],
        regions=["sea"],
        digest_limit=5,
        storage=storage,
        delivery_channel="telegram",
        delivery_destination="chat-2",
    )

    assert len(digest.alerts) == 1
    assert digest.alerts[0].headline == (
        "Grab: Grab launches driver support program in the Philippines | Another Publisher"
    )


def test_digest_ranking_prefers_resolved_publication_date_from_schema():
    builder = DigestBuilder()
    missing_provider_date = build_candidate(
        title="Grab undated provider article with scraped date",
        url="https://example.com/grab-undated",
        competitor="Grab",
        topic_group="product_launch",
        score=9,
        published_at="",
        country_hint="Philippines",
    ).to_alert()
    stale_provider_date = build_candidate(
        title="Grab provider dated article",
        url="https://example.com/grab-stale",
        competitor="Grab",
        topic_group="product_launch",
        score=9,
        published_at="2026-05-18",
        country_hint="Philippines",
    ).to_alert()
    undated_fallback = build_candidate(
        title="Grab truly undated article",
        url="https://example.com/grab-no-date",
        competitor="Grab",
        topic_group="product_launch",
        score=9,
        published_at="",
        country_hint="Philippines",
    ).to_alert()

    ranked = builder._rank_alerts(
        [undated_fallback, stale_provider_date, missing_provider_date],
        alert_schemas=[
            {
                "resolved_publication_date": date.min,
                "resolved_publication_date_source": "undated_fallback",
            },
            {
                "resolved_publication_date": date(2026, 5, 18),
                "resolved_publication_date_source": "provider",
            },
            {
                "resolved_publication_date": date(2026, 5, 20),
                "resolved_publication_date_source": "html_scraped",
            },
        ],
    )

    assert [alert.headline for alert in ranked] == [
        "Grab: Grab undated provider article with scraped date",
        "Grab: Grab provider dated article",
        "Grab: Grab truly undated article",
    ]
