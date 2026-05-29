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
        "confidence": 0.86,
    }


def test_build_alert_headline_prefers_country():
    alert = build_alert_payload()

    headline = build_alert_headline(alert)

    assert headline == "🚨 Competitor Alert — Philippines"


def test_format_alert_card_matches_brief_sections():
    alert = build_alert_payload()

    card = format_alert_card(
        alert,
        source_url=(
            "https://www.abs-cbn.com/news/business/2026/3/14/"
            "ride-hailing-apps-offer-support-programs-to-drivers-as-fuel-prices-soar"
        ),
    )

    assert "🚨 Competitor Alert — Philippines" in card
    assert "Конкурент: Grab / Move It" in card
    assert "Событие: Marketing + Policy Narrative" in card
    assert "Приоритет: MEDIUM" in card
    assert "Что произошло:" in card
    assert "Где: Philippines" in card
    assert "Источник:" in card
    assert "Почему это важно:" in card
    assert "Потенциальное влияние:" in card
    assert "Что делать:" in card
    assert card.index("Конкурент: Grab / Move It") < card.index("Событие: Marketing + Policy Narrative")
    assert card.index("Событие: Marketing + Policy Narrative") < card.index("Приоритет: MEDIUM")
    assert card.index("Приоритет: MEDIUM") < card.index("Что произошло:")
    assert card.index("Что произошло:") < card.index("Где: Philippines")
    assert card.index("Где: Philippines") < card.index("Источник:")
    assert card.index("Источник:") < card.index("Почему это важно:")
    assert card.index("Почему это важно:") < card.index("Потенциальное влияние:")
    assert card.index("Потенциальное влияние:") < card.index("Что делать:")
    assert "\n\nЧто произошло:\n" in card
    assert "\n\nИсточник:\n" in card


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
    assert "1. 🚨 Competitor Alert — Philippines" in digest
    assert "Источник:\nhttps://example.com/grab-ph" in digest


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

    assert "# Ежедневный превью-дайджест competitor tracker" in digest
    assert "- Сгенерировано: 2026-05-18T09:00:00Z" in digest
    assert "- Алертов в дайджесте: 1" in digest
    assert "### Что произошло" in digest
    assert "### Почему это важно" in digest
    assert "### Источник" in digest


def test_format_alert_card_uses_business_region_name_when_country_is_missing():
    alert = build_alert_payload()
    alert["country"] = ""
    alert["region"] = "Africa & MEA"

    card = format_alert_card(alert, source_url="https://example.com/careem-mea")

    assert "Где: Africa & MEA" in card
    assert "🚨 Competitor Alert — Africa & MEA" in card
    assert "Источник:\nhttps://example.com/careem-mea" in card
