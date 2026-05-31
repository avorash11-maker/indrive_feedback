from competitor_tracker.cli import build_delivery_alert_schemas
from competitor_tracker.models import CandidateArticle, RawArticle


def build_candidate(
    *,
    title: str,
    topic_group: str,
    snippet: str,
    score: int = 8,
    country_hint: str | None = "Philippines",
) -> CandidateArticle:
    return CandidateArticle(
        raw_article=RawArticle(
            title=title,
            url=f"https://example.com/{topic_group}",
            provider="mock_news",
            source="Example News",
            published_at="2026-05-30",
            snippet=snippet,
        ),
        competitor="Grab",
        topic_group=topic_group,
        score=score,
        matched_keywords=("launch",),
        summary=title,
        region="sea",
        country_hint=country_hint,
        language_hint="en",
        reasons=("priority_signal",),
    )


def test_build_delivery_alert_schemas_adds_product_block_for_product_sensitive_topic():
    alert = build_candidate(
        title="Grab launches airport booking feature in Manila",
        topic_group="product_features_innovation",
        snippet="Grab launches a new airport booking feature in Manila.",
    ).to_alert()

    alert_schemas, _ = build_delivery_alert_schemas(
        [alert],
        use_llm_alerts=False,
    )

    assert alert_schemas[0]["product_strategist_invoked"] is True
    assert alert_schemas[0]["product_strategist_trigger"] == "product_features_innovation"
    assert alert_schemas[0]["product_take"]
    assert alert_schemas[0]["product_risk"]
    assert alert_schemas[0]["product_follow_up"]
    assert "product_strategist" in alert_schemas[0]["agent_roles_executed"]


def test_build_delivery_alert_schemas_skips_product_block_for_marketing_only_alert():
    alert = build_candidate(
        title="Grab launches creator campaign in Manila",
        topic_group="campaign_launches",
        snippet="Grab launches a creator campaign in Manila.",
    ).to_alert()

    alert_schemas, _ = build_delivery_alert_schemas(
        [alert],
        use_llm_alerts=False,
    )

    assert "product_take" not in alert_schemas[0]
    assert "product_risk" not in alert_schemas[0]
    assert "product_follow_up" not in alert_schemas[0]
    assert alert_schemas[0]["agent_roles_executed"] == (
        "news_gatekeeper",
        "indrive_marcom_editor",
    )
