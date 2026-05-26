import sqlite3

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
                provider_diagnostics_json
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
    with sqlite3.connect(tmp_path / "tracker.db") as connection:
        row = connection.execute(
            """
            SELECT status
            FROM delivery_log
            WHERE alert_key = ? AND channel = ? AND destination = ?
            """,
            (alert.digest_key, "telegram", "12345"),
        ).fetchone()

    assert row == ("expired",)
