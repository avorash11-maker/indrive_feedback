from indrive_media.scraper import InDriveMentionScraper
from indrive_media.scraper import ProviderError
import json


def test_deduplicate_removes_exact_url_duplicates():
    articles = [
        {"title": "inDrive expands delivery in Mexico", "url": "https://example.com/a"},
        {"title": "Different title from same URL", "url": "https://example.com/a"},
        {"title": "inDrive launches safety feature", "url": "https://example.com/b"},
    ]

    unique = InDriveMentionScraper._deduplicate(articles)

    assert [item["url"] for item in unique] == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_deduplicate_removes_source_suffix_title_duplicates():
    articles = [
        {
            "title": "inDrive expands delivery service in Mexico City - Tech News",
            "url": "https://example.com/one",
        },
        {
            "title": "inDrive expands delivery service in Mexico City | Another Publisher",
            "url": "https://example.com/two",
        },
        {
            "title": "Bolt announces driver rewards in Europe",
            "url": "https://example.com/three",
        },
    ]

    unique = InDriveMentionScraper._deduplicate(articles)

    assert [item["url"] for item in unique] == [
        "https://example.com/one",
        "https://example.com/three",
    ]


def test_short_contained_title_helper_does_not_mark_duplicates_by_itself():
    assert (
        InDriveMentionScraper._is_title_contained_duplicate(
            "indrive safety",
            "indrive safety update",
        )
        is False
    )


def test_deduplicate_removes_semantically_duplicate_permit_titles():
    articles = [
        {
            "title": "inDrive Secures Operating Permits in Guadalajara, Puerto Vallarta",
            "url": "https://mexicobusiness.news/mobility/news/indrive-secures-operating-permits-guadalajara-puerto-vallarta",
        },
        {
            "title": "inDrive Secures Permit to Operate Legally in Puerto Vallarta",
            "url": "https://banderasnews.com/indrive-secures-permit-to-operate-legally-in-puerto-vallarta/",
        },
    ]

    unique = InDriveMentionScraper._deduplicate(articles)

    assert len(unique) == 1


def test_deduplicate_removes_semantically_duplicate_cashless_payment_titles():
    articles = [
        {
            "title": "dLocal Powers inDrive's Cashless Payments Rollout in South Africa with Real-Time Splits and Payouts",
            "url": "https://techafricanews.com/2026/05/05/dlocal-powers-indrives-cashless-payments-rollout-in-south-africa-with-real-time-splits-and-payouts/",
        },
        {
            "title": "dLocal and inDrive Launch Cashless Rides in South Africa",
            "url": "https://fintech.global/2026/05/05/dlocal-and-indrive-launch-cashless-rides-in-south-africa/",
        },
    ]

    unique = InDriveMentionScraper._deduplicate(articles)

    assert len(unique) == 1


def test_run_continues_when_one_provider_fails(tmp_path, monkeypatch):
    scraper = InDriveMentionScraper(output_dir=str(tmp_path), use_llm=False)

    monkeypatch.setattr(scraper, "search_newsapi", lambda query: [])

    def fail_gdelt(query):
        raise ProviderError("gdelt timeout")

    monkeypatch.setattr(scraper, "search_gdelt", fail_gdelt)
    monkeypatch.setattr(
        scraper,
        "search_google_news",
        lambda query: [
            {
                "title": "inDrive expands taxi service",
                "url": "https://example.com/news",
                "source": "Example",
                "published_at": "2026-05-14T00:00:00Z",
                "snippet": "Drivers and passengers discuss fares.",
                "collected_at": "2026-05-14T00:00:00Z",
                "query": query,
                "provider": "google_news_rss",
            }
        ],
    )
    monkeypatch.setattr("indrive_media.scraper.time.sleep", lambda _: None)

    mentions = scraper.run(queries=["indrive"])

    assert len(mentions) == 1
    assert mentions[0]["provider"] == "google_news_rss"

    run_summary = json.loads((tmp_path / "indrive_run_summary.json").read_text(encoding="utf-8"))
    assert run_summary["provider_stats"]["gdelt"]["failures"] == 1
    assert run_summary["provider_stats"]["google_news_rss"]["articles"] == 1

    report_text = (tmp_path / "indrive_pm_report.md").read_text(encoding="utf-8")
    assert "## Pipeline health" in report_text
    assert "gdelt: attempts=1, successes=0, failures=1" in report_text
