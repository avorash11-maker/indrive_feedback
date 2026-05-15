from indrive_media.notion_integration import NotionExporter


def exporter_without_api() -> NotionExporter:
    exporter = object.__new__(NotionExporter)
    exporter.property_names = {
        "number": "№",
        "title": "Название статьи",
        "date": "Дата",
        "url": "Ссылка на статью",
        "context": "Контекст",
        "pm_importance": "Почему важно для PM",
    }
    return exporter


def test_parse_date_supports_iso_z_and_rss_gmt_dates():
    assert NotionExporter.parse_date("2026-05-06T10:20:30Z") == "2026-05-06"
    assert NotionExporter.parse_date("Tue, 05 May 2026 10:00:00 GMT") == "2026-05-05"


def test_parse_date_returns_none_for_empty_or_invalid_values():
    assert NotionExporter.parse_date("") is None
    assert NotionExporter.parse_date("not a date") is None


def test_fuzzy_duplicate_logic_detects_similar_titles_with_close_dates():
    exporter = exporter_without_api()
    mention = {
        "title": "inDrive expands delivery service in Mexico City",
        "published_at": "2026-05-06T10:00:00Z",
    }
    keeper = {
        "title": "InDrive expands delivery services in Mexico City",
        "published_at": "2026-05-05T10:00:00Z",
    }

    assert exporter._is_duplicate_mention(mention, keeper) is True


def test_fuzzy_duplicate_logic_ignores_similar_titles_when_dates_are_far_apart():
    exporter = exporter_without_api()
    mention = {
        "title": "inDrive expands delivery service in Mexico City",
        "published_at": "2026-05-20T10:00:00Z",
    }
    keeper = {
        "title": "InDrive expands delivery services in Mexico City",
        "published_at": "2026-05-05T10:00:00Z",
    }

    assert exporter._is_duplicate_mention(mention, keeper) is False


def test_semantic_duplicate_logic_detects_same_permit_story():
    exporter = exporter_without_api()
    mention = {
        "title": "inDrive Secures Operating Permits in Guadalajara, Puerto Vallarta",
        "published_at": "2026-05-06",
    }
    keeper = {
        "title": "inDrive Secures Permit to Operate Legally in Puerto Vallarta",
        "published_at": "2026-05-06",
    }

    assert exporter._is_duplicate_mention(mention, keeper) is True


def test_semantic_duplicate_logic_detects_same_cashless_payment_story():
    exporter = exporter_without_api()
    mention = {
        "title": "dLocal Powers inDrive's Cashless Payments Rollout in South Africa with Real-Time Splits and Payouts",
        "published_at": "2026-05-06",
    }
    keeper = {
        "title": "dLocal and inDrive Launch Cashless Rides in South Africa",
        "published_at": "2026-05-06",
    }

    assert exporter._is_duplicate_mention(mention, keeper) is True


def test_export_mentions_updates_existing_url_and_creates_new_url(monkeypatch):
    exporter = exporter_without_api()
    calls = {"created": 0, "updated": 0, "renumbered": 0}
    existing_url = "https://example.com/existing"

    monkeypatch.setattr(
        exporter,
        "find_page_by_url",
        lambda url: "page-1" if url == existing_url else None,
    )
    monkeypatch.setattr(exporter, "create_page", lambda properties: calls.__setitem__("created", calls["created"] + 1) or {})
    monkeypatch.setattr(exporter, "update_page", lambda page_id, properties: calls.__setitem__("updated", calls["updated"] + 1) or {})
    monkeypatch.setattr(exporter, "renumber_database", lambda: calls.__setitem__("renumbered", calls["renumbered"] + 1) or 2)

    stats = exporter.export_mentions(
        [
            {"title": "Existing article", "url": existing_url, "published_at": "2026-05-06"},
            {"title": "New article", "url": "https://example.com/new", "published_at": "2026-05-05"},
        ]
    )

    assert stats["updated"] == 1
    assert stats["created"] == 1
    assert calls == {"created": 1, "updated": 1, "renumbered": 1}


def test_export_mentions_deduplicates_input_before_writing(monkeypatch):
    exporter = exporter_without_api()
    created_urls = []

    monkeypatch.setattr(exporter, "find_page_by_url", lambda url: None)
    monkeypatch.setattr(
        exporter,
        "create_page",
        lambda properties: created_urls.append(properties["Ссылка на статью"]["url"]) or {},
    )
    monkeypatch.setattr(exporter, "renumber_database", lambda: len(created_urls))

    stats = exporter.export_mentions(
        [
            {
                "title": "inDrive expands delivery service in Mexico City",
                "url": "https://example.com/one",
                "published_at": "2026-05-06",
            },
            {
                "title": "InDrive expands delivery services in Mexico City",
                "url": "https://example.com/two",
                "published_at": "2026-05-05",
            },
        ]
    )

    assert stats["created"] == 1
    assert created_urls == ["https://example.com/one"]


def test_export_mentions_dry_run_does_not_write_or_renumber(monkeypatch):
    exporter = exporter_without_api()
    calls = {"created": 0, "updated": 0, "renumbered": 0}

    monkeypatch.setattr(exporter, "find_page_by_url", lambda url: "page-1")
    monkeypatch.setattr(exporter, "create_page", lambda properties: calls.__setitem__("created", calls["created"] + 1))
    monkeypatch.setattr(exporter, "update_page", lambda page_id, properties: calls.__setitem__("updated", calls["updated"] + 1))
    monkeypatch.setattr(exporter, "renumber_database", lambda: calls.__setitem__("renumbered", calls["renumbered"] + 1))

    stats = exporter.export_mentions(
        [{"title": "Existing article", "url": "https://example.com/existing", "published_at": "2026-05-06"}],
        dry_run=True,
    )

    assert stats["would_update"] == 1
    assert stats["updated"] == 0
    assert calls == {"created": 0, "updated": 0, "renumbered": 0}


def test_archive_urls_archives_existing_rows_and_renumbers(monkeypatch):
    exporter = exporter_without_api()
    calls = {"archived": [], "renumbered": 0}

    monkeypatch.setattr(exporter, "find_page_by_url", lambda url: "page-1" if url.endswith("/bad") else None)
    monkeypatch.setattr(exporter, "archive_page", lambda page_id: calls["archived"].append(page_id) or {})
    monkeypatch.setattr(exporter, "renumber_database", lambda: calls.__setitem__("renumbered", calls["renumbered"] + 1) or 1)

    stats = exporter.archive_urls(["https://example.com/bad", "https://example.com/missing"])

    assert stats == {"archived": 1, "missing": 1}
    assert calls == {"archived": ["page-1"], "renumbered": 1}
