from competitor_tracker.formatter import (
    build_alert_headline,
    format_alert_card,
    format_daily_digest,
    format_daily_digest_markdown,
)


def build_alert_payload():
    return {
        "competitor": "Grab / Move It",
        "region": "Southeast Asia",
        "country": "Philippines",
        "topic": "Marketing + Policy Narrative",
        "event": "Driver support messaging push",
        "priority": "MEDIUM",
        "what_happened": (
            "Platforms are promoting driver support programs (fuel subsidies, bonuses) "
            "as part of public messaging."
        ),
        "why_it_matters": (
            "Driver support is becoming part of brand communication, not just operations."
        ),
        "potential_impact": (
            "Improved driver perception. Stronger trust narrative. Differentiation via driver care."
        ),
        "recommended_action": (
            "Highlight driver benefits in campaigns. Test earn more with us messaging. "
            "Align growth and comms messaging."
        ),
        "product_take": "This may raise parity expectations for driver-value communication.",
        "product_risk": "Risk of a stronger driver-care value anchor in-market.",
        "product_follow_up": "Review whether driver-facing value messaging needs a product-backed response.",
        "confidence": 0.86,
    }


def test_build_alert_headline_prefers_country():
    alert = build_alert_payload()

    headline = build_alert_headline(alert)

    assert headline == "Competitor Alert — Philippines"


def test_format_alert_card_matches_brief_sections():
    alert = build_alert_payload()

    card = format_alert_card(
        alert,
        source_url=(
            "https://www.abs-cbn.com/news/business/2026/3/14/"
            "ride-hailing-apps-offer-support-programs-to-drivers-as-fuel-prices-soar"
        ),
    )

    assert "Competitor Alert — Philippines" in card
    assert "Competitor: Grab / Move It" in card
    assert "Event: Driver support messaging push" in card
    assert "Priority: MEDIUM" in card
    assert "What happened:" in card
    assert "Where: Philippines" in card
    assert "Source:" in card
    assert "Why it matters:" in card
    assert "Potential impact:" in card
    assert "Recommended action:" not in card
    assert "Product take:" in card
    assert "Product risk:" in card
    assert "Product follow-up:" in card
    assert card.index("Competitor: Grab / Move It") < card.index("Event: Driver support messaging push")
    assert card.index("Event: Driver support messaging push") < card.index("Priority: MEDIUM")
    assert card.index("Priority: MEDIUM") < card.index("What happened:")
    assert card.index("What happened:") < card.index("Where: Philippines")
    assert card.index("Where: Philippines") < card.index("Source:")
    assert card.index("Source:") < card.index("Why it matters:")
    assert card.index("Why it matters:") < card.index("Potential impact:")
    assert card.index("Potential impact:") < card.index("Product take:")
    assert card.index("Product take:") < card.index("Product risk:")
    assert card.index("Product risk:") < card.index("Product follow-up:")
    assert "\n\nWhat happened:\n" in card
    assert "\n\nSource:\n" in card


def test_format_daily_digest_renders_local_digest_view():
    alert = build_alert_payload()

    digest = format_daily_digest(
        [alert],
        source_urls=["https://example.com/grab-ph"],
        title="Competitor Daily Digest — Morning",
        generated_at="2026-05-18T09:00:00Z",
    )

    assert "Competitor Daily Digest — Morning" in digest
    assert "Generated at: 2026-05-18T09:00:00Z" in digest
    assert "Alerts: 1" in digest
    assert "1. Competitor Alert — Philippines" in digest
    assert "Source:\nhttps://example.com/grab-ph" in digest


def test_format_daily_digest_handles_empty_alerts():
    digest = format_daily_digest([], generated_at="2026-05-18T09:00:00Z")

    assert "Alerts: 0" in digest
    assert "No alerts selected for this digest." in digest


def test_format_daily_digest_markdown_renders_russian_review_view():
    alert = build_alert_payload()

    digest = format_daily_digest_markdown(
        [alert],
        source_urls=["https://example.com/grab-ph"],
        generated_at="2026-05-18T09:00:00Z",
    )

    assert "# Competitor Tracker Digest Preview" in digest
    assert "- Generated at: 2026-05-18T09:00:00Z" in digest
    assert "- Alerts: 1" in digest
    assert "### What happened" in digest
    assert "### Why it matters" in digest
    assert "### Source" in digest
    assert "### Product take" in digest


def test_format_alert_card_uses_business_region_name_when_country_is_missing():
    alert = build_alert_payload()
    alert["country"] = ""
    alert["region"] = "Africa & MEA"

    card = format_alert_card(alert, source_url="https://example.com/careem-mea")

    assert "Where: Africa & MEA" in card
    assert "Competitor Alert — Africa & MEA" in card
    assert "Source:\nhttps://example.com/careem-mea" in card


def test_format_alert_card_can_render_russian_telegram_labels():
    alert = build_alert_payload()
    alert["product_strategist_trigger"] = "product_features_innovation"
    alert["product_strategist_invoked"] = True

    card = format_alert_card(
        alert,
        source_url="https://example.com/grab-ph",
        locale="ru",
    )

    assert "Алерт по конкуренту — Philippines" in card
    assert "Конкурент: Grab / Move It" in card
    assert "Событие: Driver support messaging push" in card
    assert "Приоритет: MEDIUM" in card
    assert "Что произошло:" in card
    assert "Где: Philippines" in card
    assert "Источник:" in card
    assert "Почему это важно:" in card
    assert "Потенциальное влияние:" in card
    assert "Продуктовый вывод:" in card
    assert "This may raise parity expectations" not in card
    assert "Этот продуктовый или сервисный запуск" in card
