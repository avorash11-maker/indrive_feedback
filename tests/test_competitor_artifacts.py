import csv
import json

from competitor_tracker.models import CandidateArticle, RawArticle
from competitor_tracker.storage import JsonFileStorage


def build_candidate() -> CandidateArticle:
    return CandidateArticle(
        raw_article=RawArticle(
            title="Grab launches a new driver support package in Manila",
            url="https://example.com/grab-driver-support",
            provider="google_news_rss",
            source="Example News",
            published_at="2026-05-19T08:00:00Z",
            snippet="Grab expands subsidies and bonus support for drivers.",
        ),
        competitor="Grab",
        topic_group="driver_support",
        score=8,
        matched_keywords=("driver support", "bonus"),
        summary="Grab is actively promoting support programs for drivers in Manila.",
        region="sea",
        country_hint="Philippines",
        language_hint="en",
        reasons=("matched competitor", "matched support keywords"),
    )


def build_alert_schema() -> dict:
    return {
        "competitor": "Grab",
        "region": "sea",
        "country": "Philippines",
        "topic": "driver_support",
        "priority": "medium",
        "what_happened": "Grab launched a new driver support package in Manila.",
        "why_it_matters": "This can strengthen driver loyalty and brand perception.",
        "potential_impact": "Higher driver retention and stronger narrative in the market.",
        "recommended_action": "Compare with local inDrive messaging and benefits.",
        "confidence": 0.82,
    }


def test_json_file_storage_saves_markdown_preview_and_csv(tmp_path):
    storage = JsonFileStorage(tmp_path)
    candidate = build_candidate()
    alert = candidate.to_alert(priority="MEDIUM", confidence=0.82)

    preview_path = storage.save_markdown_preview(
        [alert],
        [build_alert_schema()],
        generated_at="2026-05-19T09:00:00Z",
    )
    csv_path = storage.save_candidates_csv([candidate])

    preview_text = preview_path.read_text(encoding="utf-8")
    assert "Ежедневный превью-дайджест competitor tracker" in preview_text
    assert "### Что произошло" in preview_text
    assert "https://example.com/grab-driver-support" in preview_text

    with csv_path.open(encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))

    assert len(rows) == 1
    assert rows[0]["competitor"] == "Grab"
    assert rows[0]["country_hint"] == "Philippines"
    assert rows[0]["matched_keywords"] == "driver support | bonus"


def test_json_file_storage_keeps_run_summary_json_readable(tmp_path):
    storage = JsonFileStorage(tmp_path)
    from competitor_tracker.models import RunSummary

    path = storage.save_run_summary(
        RunSummary(
            started_at="2026-05-19T09:00:00Z",
            finished_at="2026-05-19T09:05:00Z",
            regions=("sea",),
            providers=("google_news_rss", "gdelt"),
            queries_generated=12,
            raw_articles_collected=9,
            candidates_kept=4,
            alerts_created=2,
            daily_digest_limit=10,
            provider_errors={"gdelt": "timeout"},
        )
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["queries_generated"] == 12
    assert payload["alerts_created"] == 2
    assert payload["provider_errors"]["gdelt"] == "timeout"
