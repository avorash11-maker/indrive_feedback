from competitor_tracker.agent_contracts import (
    AGENT_CONTRACT_VERSION,
    AgentRolePipeline,
    AgentRuntimeContext,
    ProductStrategistRole,
    default_agent_role_contracts,
)
from competitor_tracker.models import ArticleContext, CandidateArticle, RawArticle


def build_candidate() -> CandidateArticle:
    return CandidateArticle(
        raw_article=RawArticle(
            title="Grab launches campaign in Manila",
            url="https://example.com/grab-campaign",
            provider="mock",
            source="Example",
            published_at="2026-05-20",
            snippet="Grab launches a campaign in Manila.",
        ),
        competitor="Grab",
        topic_group="campaign_launches",
        score=8,
        region="sea",
        country_hint="Philippines",
        reasons=("priority_signal",),
        matched_keywords=("campaign",),
        summary="Grab / campaign_launches / Philippines: Grab launches campaign in Manila",
    )


def test_default_agent_role_contracts_define_expected_boundaries():
    contracts = default_agent_role_contracts()

    assert set(contracts) == {
        "news_gatekeeper",
        "indrive_marcom_editor",
        "product_strategist",
    }
    assert contracts["news_gatekeeper"].execution_layer == "deterministic_guardrail"
    assert contracts["indrive_marcom_editor"].agent_owned_fields == (
        "event",
        "what_happened",
        "why_it_matters",
        "potential_impact",
        "recommended_action",
        "confidence",
    )
    assert contracts["product_strategist"].execution_layer == "agent_runtime"
    assert "competitor" in contracts["indrive_marcom_editor"].pipeline_owned_fields
    assert "resolved_publication_date" in contracts["indrive_marcom_editor"].pipeline_owned_fields
    assert contracts["product_strategist"].agent_owned_fields == (
        "product_take",
        "product_risk",
        "product_follow_up",
        "product_strategist_invoked",
        "product_strategist_trigger",
    )


def test_agent_role_pipeline_adds_contract_metadata_without_mutating_alert_shape():
    captured = {"use_llm": None, "context_body": None}

    class FakeAnalyzer:
        def __init__(self, use_llm):
            self.use_llm = use_llm

        def analyze_candidate(self, candidate, *, article_context=None):
            captured["use_llm"] = self.use_llm
            captured["context_body"] = article_context.article_body if article_context else ""
            return {
                "competitor": candidate.competitor,
                "region": candidate.region or "",
                "country": candidate.country_hint or "",
                "topic": candidate.topic_group,
                "priority": "HIGH",
                "published_date": "2026-05-20",
                "published_date_source": "provider",
                "what_happened": "Signal detected.",
                "why_it_matters": "Narrative matters.",
                "potential_impact": "Trust impact.",
                "recommended_action": "Respond locally.",
                "confidence": 0.8,
            }

    pipeline = AgentRolePipeline(
        analyzer_factory=lambda use_llm, config: FakeAnalyzer(use_llm)
    )
    context = ArticleContext(
        title="Grab launches campaign in Manila",
        snippet="Grab launches a campaign in Manila.",
        source_url="https://example.com/grab-campaign",
        article_body="Extended article body.",
        published_at="2026-05-20",
        published_at_source="html_scraped",
    )

    alert = pipeline.build_alert_schema(
        build_candidate(),
        article_context=context,
        runtime=AgentRuntimeContext(ranking_index=0, llm_limit=1),
    )

    assert captured == {
        "use_llm": True,
        "context_body": "Extended article body.",
    }
    assert alert["what_happened"] == "Signal detected."
    assert alert["agent_contract_version"] == AGENT_CONTRACT_VERSION
    assert alert["agent_roles_executed"] == (
        "news_gatekeeper",
        "indrive_marcom_editor",
    )
    assert alert["truth_layer_owner"] == "deterministic_pipeline"


def test_product_strategist_invokes_for_product_sensitive_topic():
    strategist = ProductStrategistRole()
    candidate = CandidateArticle(
        raw_article=RawArticle(
            title="Grab launches airport booking feature in Manila",
            url="https://example.com/grab-airport-feature",
            provider="mock",
            source="Example",
            published_at="2026-05-20",
            snippet="Grab launches an airport booking feature in Manila.",
        ),
        competitor="Grab",
        topic_group="product_features_innovation",
        score=8,
        region="sea",
        country_hint="Philippines",
        reasons=("priority_signal",),
        matched_keywords=("feature",),
        summary="Grab launches airport booking feature in Manila",
    )
    alert = strategist.review_alert(
        {
            "competitor": "Grab",
            "region": "SEA",
            "country": "Philippines",
            "topic": "product features innovation",
            "event": "Airport booking feature launch",
            "priority": "HIGH",
            "what_happened": "Grab launched an airport booking feature.",
            "why_it_matters": "This may shape product expectations.",
            "potential_impact": "Potential adoption impact.",
            "recommended_action": "Review locally.",
            "confidence": 0.8,
        },
        candidate=candidate,
    )

    assert alert["product_strategist_invoked"] is True
    assert alert["product_strategist_trigger"] == "product_features_innovation"
    assert "parity" in alert["product_risk"].lower() or "feature-gap" in alert["product_risk"].lower()
    assert alert["product_take"]
    assert alert["product_follow_up"]


def test_product_strategist_does_not_invoke_for_marketing_only_alert():
    strategist = ProductStrategistRole()
    candidate = build_candidate()
    base_alert = {
        "competitor": "Grab",
        "region": "SEA",
        "country": "Philippines",
        "topic": "campaign launches",
        "event": "Driver campaign launch",
        "priority": "HIGH",
        "what_happened": "Grab launched a campaign.",
        "why_it_matters": "Narrative matters.",
        "potential_impact": "Trust impact.",
        "recommended_action": "Respond locally.",
        "confidence": 0.8,
    }

    assert strategist.should_invoke(candidate, alert_schema=base_alert) is False
    alert = strategist.review_alert(base_alert, candidate=candidate)

    assert "product_take" not in alert
    assert "product_risk" not in alert
    assert "product_follow_up" not in alert
