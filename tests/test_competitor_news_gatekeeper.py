from competitor_tracker.analyzer import AnalysisResult
from competitor_tracker.cli import run_pipeline
from competitor_tracker.config import TrackerConfig, TrackerRuntimeConfig
from competitor_tracker.models import CandidateArticle, RawArticle
from competitor_tracker.news_gatekeeper import NewsGatekeeper


def build_config() -> TrackerConfig:
    return TrackerConfig.load_default()


def build_candidate(
    *,
    title: str,
    snippet: str,
    competitor: str = "Grab",
    topic_group: str = "campaign_launches",
    score: int = 7,
    region: str = "sea",
    country_hint: str | None = "Philippines",
    url: str = "https://example.com/story",
    source: str = "Example News",
) -> CandidateArticle:
    return CandidateArticle(
        raw_article=RawArticle(
            title=title,
            url=url,
            provider="google_news_rss",
            source=source,
            published_at="2026-05-20",
            snippet=snippet,
        ),
        competitor=competitor,
        topic_group=topic_group,
        score=score,
        matched_keywords=("campaign",),
        summary=title,
        region=region,
        country_hint=country_hint,
        language_hint="en",
        reasons=("priority_signal",),
    )


def test_news_gatekeeper_accepts_concrete_market_move():
    candidate = build_candidate(
        title="Grab launches driver campaign and promo code in Manila",
        snippet="Grab launched a campaign with promo code support and driver recruitment in Manila.",
        score=8,
    )

    decision = NewsGatekeeper(config=build_config()).evaluate(candidate)

    assert decision.accepted is True
    assert decision.canonical_topic == "campaign_launches"
    assert decision.priority_hint in {"MEDIUM", "HIGH"}
    assert "concrete" in decision.relevance_reason.lower()


def test_news_gatekeeper_rejects_broad_industry_fluff():
    candidate = build_candidate(
        title="Global ride hailing market size forecast 2030 mentions Grab",
        snippet="A new industry analysis covers market size, outlook, and long-term forecast.",
        topic_group="core_industry_terms",
        score=6,
    )

    decision = NewsGatekeeper(config=build_config()).evaluate(candidate)

    assert decision.accepted is False
    assert decision.canonical_topic == "core_industry_terms"
    assert decision.rejection_reason == "broad_industry_fluff"


def test_news_gatekeeper_rejects_mention_only_story():
    candidate = build_candidate(
        title="Top brands in mobility: Grab among the brands discussed",
        snippet="Grab was mentioned among the brands in a broad mobility roundup.",
        topic_group="campaign_launches",
        score=5,
        country_hint=None,
    )

    decision = NewsGatekeeper(config=build_config()).evaluate(candidate)

    assert decision.accepted is False
    assert decision.rejection_reason == "mention_only_story"


def test_news_gatekeeper_rejects_off_scope_visual_or_social_scraping_story():
    candidate = build_candidate(
        title="Grab social listening dashboard tracks logo redesign performance",
        snippet="The write-up is about scraped from Instagram creative monitoring and logo redesign assets.",
        topic_group="campaign_launches",
        score=7,
    )

    decision = NewsGatekeeper(config=build_config()).evaluate(candidate)

    assert decision.accepted is False
    assert decision.rejection_reason == "off_scope_material"


def test_news_gatekeeper_filter_analysis_updates_drops_and_metadata():
    accepted = build_candidate(
        title="Grab expands airport pickup service in Manila",
        snippet="Grab expands airport pickup service and launches a rider promo in Manila.",
        topic_group="market_expansion",
        score=8,
        url="https://example.com/accepted",
    )
    rejected = build_candidate(
        title="Ride hailing market forecast 2030 mentions Grab",
        snippet="A broad industry analysis covers market outlook and market size.",
        topic_group="core_industry_terms",
        score=6,
        url="https://example.com/rejected",
    )

    result = NewsGatekeeper(config=build_config()).filter_analysis(
        AnalysisResult(candidates=[accepted, rejected], dropped_count=0, dropped_articles=[])
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].raw_article.metadata["news_gatekeeper_accept"] is True
    assert result.candidates[0].raw_article.metadata["news_gatekeeper_canonical_topic"] == "market_expansion"
    assert result.dropped_count == 1
    assert result.dropped_articles[0].reason == "broad_industry_fluff"
    assert result.dropped_articles[0].details["agent_role"] == "news_gatekeeper"


def test_news_gatekeeper_accepts_text_described_ugc_or_tiktok_strategy():
    candidate = build_candidate(
        title="Grab launches creator campaign with TikTok strategy in Manila",
        snippet="The article describes a creator campaign, UGC push, and TikTok strategy as part of a local launch.",
        topic_group="campaign_launches",
        score=8,
    )

    decision = NewsGatekeeper(config=build_config()).evaluate(candidate)

    assert decision.accepted is True
    assert "text-described marketing activity" in decision.relevance_reason


def test_run_pipeline_news_gatekeeper_rejects_noise_before_digest(tmp_path, monkeypatch):
    runtime = TrackerRuntimeConfig(
        config_path=TrackerRuntimeConfig().config_path,
        output_dir=tmp_path / "output",
        database_path=tmp_path / "tracker.db",
        use_llm_alerts=False,
        llm_top_n=0,
        telegram_top_n=5,
        article_context_max_chars=1000,
    )

    monkeypatch.setattr("competitor_tracker.cli.TrackerRuntimeConfig.from_env", staticmethod(lambda: runtime))

    raw_articles = [
        RawArticle(
            title="Grab launches strategic partnership campaign in Manila",
            url="https://example.com/high-signal",
            provider="mock_news",
            source="Example News",
            published_at="2026-05-30",
            snippet="Grab launches strategic partnership campaign and rider promo in Manila.",
        ),
        RawArticle(
            title="Global ride hailing market size forecast 2030 mentions Grab",
            url="https://example.com/noise",
            provider="mock_news",
            source="Example News",
            published_at="2026-05-30",
            snippet="A new industry analysis covers market size and market outlook.",
        ),
    ]

    def fake_collect_raw_articles(**kwargs):
        return raw_articles, ("mock_news",), {}

    def fake_prefilter(self, raw_articles, regions=None):
        return AnalysisResult(
            candidates=[
                CandidateArticle(
                    raw_article=raw_articles[0],
                    competitor="Grab",
                    topic_group="campaign_launches",
                    score=9,
                    matched_keywords=("campaign", "partnership"),
                    summary="Grab strategic partnership campaign in Manila",
                    region="sea",
                    country_hint="Philippines",
                    language_hint="en",
                    reasons=("competitor_mentioned", "priority_signal"),
                ),
                CandidateArticle(
                    raw_article=raw_articles[1],
                    competitor="Grab",
                    topic_group="core_industry_terms",
                    score=6,
                    matched_keywords=("ride-hailing",),
                    summary="Global ride hailing market size forecast 2030 mentions Grab",
                    region="sea",
                    country_hint=None,
                    language_hint="en",
                    reasons=("competitor_mentioned",),
                ),
            ],
            dropped_count=0,
            dropped_articles=[],
        )

    monkeypatch.setattr("competitor_tracker.cli.collect_raw_articles", fake_collect_raw_articles)
    monkeypatch.setattr("competitor_tracker.cli.CompetitorAnalyzer.prefilter_raw_articles", fake_prefilter)

    result = run_pipeline(days=7, min_score=5, regions=["sea"])

    assert len(result["analysis"].candidates) == 2
    assert len(result["gatekeeper_analysis"].candidates) == 1
    assert len(result["digest"].alerts) == 1
    assert result["digest"].alerts[0].candidate.title == "Grab launches strategic partnership campaign in Manila"
    assert result["gatekeeper_analysis"].dropped_articles[-1].reason == "broad_industry_fluff"
    assert result["alert_schemas"][0]["news_gatekeeper_accept"] is True
    assert result["alert_schemas"][0]["news_gatekeeper_canonical_topic"] == "campaign_launches"
