import sqlite3
from datetime import datetime, timezone

from competitor_tracker.models import CandidateArticle, DeliveryRecord, RawArticle, RunSummary
from competitor_tracker.storage import SQLiteTrackerStorage


def build_raw_article() -> RawArticle:
    return RawArticle(
        title="Uber expands premium rides in Mexico City",
        url="https://example.com/uber-premium-mx",
        provider="newsapi",
        source="Example News",
        published_at="2026-05-18T09:00:00Z",
        snippet="Premium category rollout in Mexico City.",
        query="\"Uber\" launch Mexico",
        region="latam",
        language="en",
        competitor_hints=("Uber",),
        metadata={"source_type": "news"},
    )


def build_candidate() -> CandidateArticle:
    return CandidateArticle(
        raw_article=build_raw_article(),
        competitor="Uber",
        topic_group="product_launch",
        score=8,
        matched_keywords=("launch", "premium"),
        summary="Uber launched a premium tier in Mexico City.",
        region="latam",
        language_hint="en",
        reasons=("new product rollout",),
    )


def test_sqlite_storage_inserts_and_detects_seen_articles(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")
    article = build_raw_article()

    first_id = storage.insert_raw_article(article)
    second_id = storage.insert_raw_article(article)

    assert first_id == second_id
    assert storage.has_seen_article(article.url) is True

    with sqlite3.connect(tmp_path / "tracker.db") as connection:
        count = connection.execute("SELECT COUNT(*) FROM articles_raw").fetchone()[0]
    assert count == 1


def test_sqlite_storage_inserts_candidates_alerts_and_runs(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")
    candidate = build_candidate()
    alert = candidate.to_alert(severity="high", channels=("email", "slack"))
    summary = RunSummary(
        started_at="2026-05-18T09:00:00Z",
        finished_at="2026-05-18T09:05:00Z",
        regions=("latam",),
        providers=("newsapi",),
        queries_generated=4,
        raw_articles_collected=1,
        candidates_kept=1,
        alerts_created=1,
        daily_digest_limit=12,
        raw_articles_fetched=2,
        raw_articles_deduplicated=1,
        articles_filtered_out=0,
        alerts_sent=1,
        status="success",
        drop_reasons={"score_below_threshold": 0},
        provider_diagnostics={"gdelt": {"status": "ok", "items_found": 2}},
        provider_metrics={"gdelt": {"cache_hits": 1, "source_tier_wins": 2}},
    )

    candidate_id = storage.insert_candidate(candidate)
    alert_id = storage.insert_alert(alert)
    run_id = storage.insert_run(summary)

    assert candidate_id > 0
    assert alert_id > 0
    assert run_id > 0

    with sqlite3.connect(tmp_path / "tracker.db") as connection:
        candidate_count = connection.execute(
            "SELECT COUNT(*) FROM article_candidates"
        ).fetchone()[0]
        alert_count = connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        run_count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    assert candidate_count == 1
    assert alert_count == 1
    assert run_count == 1

    with sqlite3.connect(tmp_path / "tracker.db") as connection:
        row = connection.execute(
            """
            SELECT
                raw_articles_fetched,
                raw_articles_deduplicated,
                articles_filtered_out,
                alerts_sent,
                status,
                drop_reasons_json,
                provider_errors_json,
                provider_diagnostics_json,
                provider_metrics_json
            FROM runs
            LIMIT 1
            """
        ).fetchone()

    assert row == (
        2,
        1,
        0,
        1,
        "success",
        '{"score_below_threshold": 0}',
        '{}',
        '{"gdelt": {"items_found": 2, "status": "ok"}}',
        '{"gdelt": {"cache_hits": 1, "source_tier_wins": 2}}',
    )


def test_sqlite_storage_checks_and_marks_delivered(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")
    alert = build_candidate().to_alert()

    delivery_id = storage.log_delivery(
        DeliveryRecord(
            alert_key=alert.digest_key,
            channel="email",
            status="pending",
            destination="digest@example.com",
        )
    )

    assert delivery_id > 0
    assert storage.has_sent_alert(
        alert.digest_key, "email", "digest@example.com"
    ) is False

    same_delivery_id = storage.mark_delivered(
        alert_key=alert.digest_key,
        channel="email",
        delivered_at="2026-05-18T09:06:00Z",
        destination="digest@example.com",
        external_id="msg-123",
        metadata={"batch": "morning"},
    )

    assert same_delivery_id == delivery_id
    assert storage.has_sent_alert(
        alert.digest_key, "email", "digest@example.com"
    ) is True

    with sqlite3.connect(tmp_path / "tracker.db") as connection:
        row = connection.execute(
            """
            SELECT status, delivered_at, external_id
            FROM delivery_log
            WHERE alert_key = ? AND channel = ? AND destination = ?
            """,
            (alert.digest_key, "email", "digest@example.com"),
        ).fetchone()

    assert row == ("delivered", "2026-05-18T09:06:00Z", "msg-123")


def test_sqlite_storage_returns_only_recent_undelivered_deferred_candidates(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")

    recent_undelivered = build_candidate()
    delivered = CandidateArticle(
        raw_article=RawArticle(
            title="Uber expands courier service in Bogota",
            url="https://example.com/uber-courier-co",
            provider="newsapi",
            source="Example News",
            published_at="2026-05-18T10:00:00Z",
            snippet="Courier launch in Colombia.",
            query="\"Uber\" courier Colombia",
            region="latam",
            language="en",
            competitor_hints=("Uber",),
            metadata={"source_type": "news"},
        ),
        competitor="Uber",
        topic_group="product_launch",
        score=9,
        matched_keywords=("launch",),
        summary="Uber courier launch in Colombia.",
        region="latam",
        language_hint="en",
        reasons=("new product rollout",),
    )
    expired = CandidateArticle(
        raw_article=RawArticle(
            title="Uber old alert in Guadalajara",
            url="https://example.com/uber-old-mx",
            provider="newsapi",
            source="Example News",
            published_at="2026-05-15T09:00:00Z",
            snippet="Older signal.",
            query="\"Uber\" old Mexico",
            region="latam",
            language="en",
            competitor_hints=("Uber",),
            metadata={"source_type": "news"},
        ),
        competitor="Uber",
        topic_group="product_launch",
        score=7,
        matched_keywords=("launch",),
        summary="Older Uber signal.",
        region="latam",
        language_hint="en",
        reasons=("older",),
    )

    recent_alert = recent_undelivered.to_alert()
    delivered_alert = delivered.to_alert()
    expired_alert = expired.to_alert()
    storage.insert_alert(recent_alert)
    storage.insert_alert(delivered_alert)
    storage.insert_alert(expired_alert)
    storage.mark_deferred(
        alert_key=recent_alert.digest_key,
        channel="telegram",
        destination="12345",
    )
    storage.mark_deferred(
        alert_key=expired_alert.digest_key,
        channel="telegram",
        destination="12345",
    )

    storage.mark_delivered(
        alert_key=delivered_alert.digest_key,
        channel="telegram",
        delivered_at="2026-05-18T10:05:00Z",
        destination="12345",
    )

    with sqlite3.connect(tmp_path / "tracker.db") as connection:
        connection.execute(
            "UPDATE alerts SET created_at = datetime('now', '-3 days') WHERE digest_key = ?",
            (expired_alert.digest_key,),
        )
        connection.commit()

    deferred = storage.get_deferred_candidates(
        channel="telegram",
        destination="12345",
        max_age_days=2,
    )

    assert [candidate.url for candidate in deferred] == [recent_undelivered.url]


def test_sqlite_storage_marks_deferred_and_expires_stale_entries(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")
    alert = build_candidate().to_alert()
    storage.insert_alert(alert)

    delivery_id = storage.mark_deferred(
        alert_key=alert.digest_key,
        channel="telegram",
        destination="12345",
        metadata={"mode": "daily_digest"},
    )
    assert delivery_id > 0

    with sqlite3.connect(tmp_path / "tracker.db") as connection:
        connection.execute(
            "UPDATE alerts SET created_at = datetime('now', '-3 days') WHERE digest_key = ?",
            (alert.digest_key,),
        )
        connection.commit()

    expired_count = storage.expire_stale_deferred(
        channel="telegram",
        destination="12345",
        max_age_days=2,
    )

    assert expired_count == 1


def test_sqlite_storage_scores_query_precision_from_history(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")

    strong_raw = RawArticle(
        title="Uber expands premium rides in Mexico City",
        url="https://example.com/uber-precision",
        provider="guardian",
        source="Example News",
        published_at="2026-05-18T09:00:00Z",
        snippet="Strong signal.",
        query='"Uber" market expansion Latin America',
        region="latam",
        language="en",
        competitor_hints=("Uber",),
        metadata={"source_type": "news"},
    )
    weak_raw = RawArticle(
        title="DiDi appears in industry roundup",
        url="https://example.com/didi-precision",
        provider="guardian",
        source="Example News",
        published_at="2026-05-18T09:00:00Z",
        snippet="Weak signal.",
        query='"DiDi" market expansion Latin America',
        region="latam",
        language="en",
        competitor_hints=("DiDi",),
        metadata={"source_type": "news"},
    )
    strong_candidate = CandidateArticle(
        raw_article=strong_raw,
        competitor="Uber",
        topic_group="market_expansion",
        score=9,
        matched_keywords=("launch",),
        summary="Strong signal.",
        region="latam",
        language_hint="en",
        reasons=("high precision",),
    )

    storage.insert_raw_article(strong_raw)
    storage.insert_candidate(strong_candidate)
    storage.insert_alert(strong_candidate.to_alert())
    storage.insert_raw_article(weak_raw)

    precision = storage.get_query_precision_by_text(
        [
            '"Uber" market expansion Latin America',
            '"DiDi" market expansion Latin America',
        ]
    )

    assert precision['"Uber" market expansion Latin America'] > precision['"DiDi" market expansion Latin America']


def test_sqlite_storage_applies_time_decay_to_historical_precision(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")
    recent_query = '"Uber" market expansion Latin America'
    old_query = '"DiDi" market expansion Latin America'
    reference_now = datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc)

    recent_raw = RawArticle(
        title="Uber expands in Mexico City",
        url="https://example.com/recent-uber",
        provider="guardian",
        source="Example News",
        published_at="2026-05-25T09:00:00Z",
        snippet="Recent strong signal.",
        query=recent_query,
        region="latam",
        language="en",
        competitor_hints=("Uber",),
        metadata={"source_type": "news"},
    )
    old_raw = RawArticle(
        title="DiDi expands in Mexico City",
        url="https://example.com/old-didi",
        provider="guardian",
        source="Example News",
        published_at="2026-03-01T09:00:00Z",
        snippet="Older strong signal.",
        query=old_query,
        region="latam",
        language="en",
        competitor_hints=("DiDi",),
        metadata={"source_type": "news"},
    )

    recent_candidate = CandidateArticle(
        raw_article=recent_raw,
        competitor="Uber",
        topic_group="market_expansion",
        score=9,
        matched_keywords=("launch",),
        summary="Recent strong signal.",
        region="latam",
        language_hint="en",
        reasons=("recent",),
    )
    old_candidate = CandidateArticle(
        raw_article=old_raw,
        competitor="DiDi",
        topic_group="market_expansion",
        score=9,
        matched_keywords=("launch",),
        summary="Older strong signal.",
        region="latam",
        language_hint="en",
        reasons=("old",),
    )

    recent_raw_id = storage.insert_raw_article(recent_raw)
    storage.insert_candidate(recent_candidate)
    storage.insert_alert(recent_candidate.to_alert())
    old_raw_id = storage.insert_raw_article(old_raw)
    storage.insert_candidate(old_candidate)
    storage.insert_alert(old_candidate.to_alert())

    with sqlite3.connect(tmp_path / "tracker.db") as connection:
        connection.execute(
            "UPDATE articles_raw SET created_at = ? WHERE id = ?",
            ("2026-05-25 00:00:00", recent_raw_id),
        )
        connection.execute(
            "UPDATE articles_raw SET created_at = ? WHERE id = ?",
            ("2026-03-01 00:00:00", old_raw_id),
        )
        connection.commit()

    precision = storage.get_query_precision_by_text(
        [recent_query, old_query],
        half_life_days=14,
        now=reference_now,
    )

    assert precision[recent_query] > precision[old_query]


def test_sqlite_storage_persists_and_reports_feed_health_metrics(tmp_path):
    storage = SQLiteTrackerStorage(tmp_path / "tracker.db")

    inserted_ids = storage.insert_feed_metric_rows(
        run_id=None,
        rows=[
            {
                "measured_at": "2026-05-26T10:00:00+00:00",
                "provider": "regional_rss",
                "region": "latam",
                "feed_name": "MercoPress",
                "feed_url": "https://en.mercopress.com/rss",
                "items_found": 12,
                "provider_matches": 6,
                "raw_articles_after_global_dedup": 4,
                "prefilter_passed": 1,
                "candidates_kept": 1,
                "alerts_created": 0,
                "dropped_prefilter": 3,
                "noise_ratio": 0.8333,
                "recommendation": "review_low_alert_yield",
            },
            {
                "measured_at": "2026-05-26T10:00:00+00:00",
                "provider": "regional_rss",
                "region": "mea",
                "feed_name": "Doha News",
                "feed_url": "https://dohanews.co/feed/",
                "items_found": 10,
                "provider_matches": 4,
                "raw_articles_after_global_dedup": 3,
                "prefilter_passed": 2,
                "candidates_kept": 2,
                "alerts_created": 1,
                "dropped_prefilter": 1,
                "noise_ratio": 0.5,
                "recommendation": "keep",
            },
        ],
    )

    assert len(inserted_ids) == 2

    report = storage.get_feed_health_report(days=365, min_items_found=1, limit=10)

    assert report["feed_count"] == 2
    assert report["highest_noise_feed"]["feed_name"] == "MercoPress"
    assert report["feeds"][0]["recommendation"] == "review_low_signal"
    assert any(
        item["feed_name"] == "MercoPress"
        for item in report["recommendations"]
    )
