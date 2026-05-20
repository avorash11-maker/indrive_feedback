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
